package aistudio

import (
        "bufio"
        "context"
        "crypto/rand"
        "encoding/binary"
        "fmt"
        "io"
        "log/slog"
        "sync"
        "sync/atomic"
        "time"

        quic "github.com/Mag1cFall/uquic"
        "github.com/Mag1cFall/uquic/quicvarint"
)

// http3Conn 是一条按 neqo 0.28 行为收发的 HTTP/3 连接
type http3Conn struct {
        quic *quic.Conn
        // mu 保护 QPACK 编码器，并保证编码器流指令先于引用它的头块写出
        mu            sync.Mutex
        encoder       *qpackEncoder
        encoderStream *quic.SendStream
        decoder       *qpackDecoder
        decoderMu     sync.Mutex
        decoderStream *quic.SendStream
        ackPending    atomic.Bool
        goaway        chan struct{}
        goawayOnce    sync.Once
        // ready 在握手完成后关闭，0-RTT 被拒绝时在重新打开控制流与 QPACK 流之后关闭
        ready chan struct{}
        // remember 记录服务端 SETTINGS 中的 QPACK 设置，供下次 0-RTT 沿用
        remember func(capacity uint64, blocked uint64)
}

// newHTTP3Conn 在 QUIC 连接上按 neqo 的顺序打开控制流、QPACK 编码器流与解码器流；settings 非 nil 表示握手尚未完成的 0-RTT 连接
func newHTTP3Conn(connection *quic.Conn, settings *[2]uint64, remember func(capacity uint64, blocked uint64)) (*http3Conn, error) {
        conn := &http3Conn{quic: connection, goaway: make(chan struct{}), ready: make(chan struct{}), remember: remember}
        if err := conn.open(settings); err != nil {
                conn.close()
                return nil, err
        }
        // 单一 accept 循环：连接生命周期内只起一条（用 conn.quic.Context()
        // 作退出条件）。原先每次 open 都各起一条循环，0-RTT 被拒绝时两条
        // 循环对同一 AcceptUniStream 竞争——服务端的控制/QPACK 流被旧循环
        // 抢走时，新代编码器/解码器永远学不到对端参数（容量恒 0 或解码
        // 永久阻塞）。改为接受时从 conn 现取当前代实例（流级快照）。
        go conn.acceptUniStreams()
        if settings == nil {
                close(conn.ready)
        } else {
                go conn.awaitHandshake()
        }
        return conn, nil
}

// open 打开控制流、QPACK 编码器流与解码器流并写出各流开头，编码器与解码器从初始状态开始
func (conn *http3Conn) open(settings *[2]uint64) error {
        encoder, decoder := newQPACKEncoder(), newQPACKDecoder()
        if settings != nil {
                encoder.setPeerSettings(settings[0], settings[1])
        }
        var encoderStream, decoderStream *quic.SendStream
        control, err := conn.quic.OpenUniStream()
        if err == nil {
                _, err = control.Write(firefoxControlStream())
        }
        if err == nil {
                encoderStream, err = conn.quic.OpenUniStream()
        }
        if err == nil {
                decoderStream, err = conn.quic.OpenUniStream()
        }
        if err != nil {
                return err
        }
        conn.mu.Lock()
        conn.encoder, conn.encoderStream = encoder, encoderStream
        err = conn.flushEncoderLocked()
        conn.mu.Unlock()
        conn.decoderMu.Lock()
        conn.decoder, conn.decoderStream = decoder, decoderStream
        conn.decoderMu.Unlock()
        return conn.flushDecoder()
}

// awaitHandshake 等 0-RTT 连接握手完成；服务端拒绝 0-RTT 时按 neqo 重新打开控制流与 QPACK 流，编码器等待新的服务端 SETTINGS
func (conn *http3Conn) awaitHandshake() {
        defer close(conn.ready)
        select {
        case <-conn.quic.HandshakeComplete():
        case <-conn.quic.Context().Done():
                return
        }
        if conn.quic.ConnectionState().Used0RTT {
                return
        }
        if _, err := conn.quic.NextConnection(conn.quic.Context()); err != nil || conn.open(nil) != nil {
                conn.close()
        }
}

// awaitReady 等待连接握手完成
func (conn *http3Conn) awaitReady(ctx context.Context) error {
        select {
        case <-conn.ready:
                return nil
        case <-ctx.Done():
                return ctx.Err()
        }
}

// firefoxControlStream 返回控制流开头的流类型、SETTINGS 与一个随机 GREASE 帧
func firefoxControlStream() []byte {
        var settings []byte
        for _, setting := range firefoxHTTP3Settings {
                settings = quicvarint.Append(settings, setting[0])
                settings = quicvarint.Append(settings, setting[1])
        }
        stream := quicvarint.Append(nil, h3ControlStreamType)
        stream = appendH3Frame(stream, h3FrameSettings, settings)
        var random [8]byte
        _, _ = rand.Read(random[:])
        greaseType := (binary.NativeEndian.Uint64(random[:])>>7)*0x1f + 0x21
        return appendH3Frame(stream, greaseType, random[1:1+random[0]&0x7])
}

func appendH3Frame(buffer []byte, kind uint64, payload []byte) []byte {
        buffer = quicvarint.Append(buffer, kind)
        buffer = quicvarint.Append(buffer, uint64(len(payload)))
        return append(buffer, payload...)
}

func (conn *http3Conn) flushEncoderLocked() error {
        if len(conn.encoder.pending) == 0 {
                return nil
        }
        _, err := conn.encoderStream.Write(conn.encoder.pending)
        conn.encoder.pending = nil
        return err
}

func (conn *http3Conn) flushDecoder() error {
        conn.decoderMu.Lock()
        defer conn.decoderMu.Unlock()
        if pending := conn.decoder.takePending(); len(pending) > 0 {
                _, err := conn.decoderStream.Write(pending)
                return err
        }
        return nil
}

// scheduleDecoderFlush 在本批编码器指令处理完的 1 毫秒后写出 Insert Count Increment，期间解码的头块先写出 Section Acknowledgment
func (conn *http3Conn) scheduleDecoderFlush() {
        if conn.ackPending.CompareAndSwap(false, true) {
                time.AfterFunc(time.Millisecond, func() {
                        conn.ackPending.Store(false)
                        if err := conn.flushDecoder(); err != nil {
                                // 写失败（连接垂死）时 QPACK 回执丢失：服务端编码器
                                // 等不到 Insert Count Increment/Section Ack 会冻结动态
                                // 表退化为全字面量。连接本身的拆除交给后续流读失败
                                // 的 CloseWithError 兜底，这里至少留下可排查日志。
                                slog.Warn("HTTP/3 解码器流回执写入失败", "error", err)
                        }
                })
        }
}

// acceptUniStreams 处理服务端打开的单向流：控制流、QPACK 编码器流与解码器流，其余类型以 H3_STREAM_CREATION_ERROR 停止接收。
// 每条流在处理开始时从 conn 取当前代实例（流级快照）：0-RTT 被拒绝后 open(nil) 换新编码器/解码器，后到的服务端流必须
// 挂到新代上，否则新代编码器学不到服务端 QPACK SETTINGS（容量恒 0）、解码器等不到动态表插入（永久阻塞）。
func (conn *http3Conn) acceptUniStreams() {
        for {
                stream, err := conn.quic.AcceptUniStream(conn.quic.Context())
                if err != nil {
                        return
                }
                go func() {
                        reader := bufio.NewReader(stream)
                        kind, err := quicvarint.Read(reader)
                        if err != nil {
                                return
                        }
                        switch kind {
                        case h3ControlStreamType:
                                conn.mu.Lock()
                                encoder := conn.encoder
                                conn.mu.Unlock()
                                if encoder == nil {
                                        stream.CancelRead(h3StreamCreationError)
                                        return
                                }
                                err = conn.readControlStream(reader, encoder)
                        case qpackEncoderStreamType:
                                conn.decoderMu.Lock()
                                decoder := conn.decoder
                                conn.decoderMu.Unlock()
                                if decoder == nil {
                                        stream.CancelRead(h3StreamCreationError)
                                        return
                                }
                                err = decoder.readEncoderStream(reader, conn.scheduleDecoderFlush)
                        case qpackDecoderStreamType:
                                conn.mu.Lock()
                                encoder := conn.encoder
                                conn.mu.Unlock()
                                if encoder == nil {
                                        stream.CancelRead(h3StreamCreationError)
                                        return
                                }
                                err = encoder.readDecoderStream(reader, &conn.mu)
                        default:
                                stream.CancelRead(h3StreamCreationError)
                                return
                        }
                        if err != nil && conn.quic.Context().Err() == nil {
                                _ = conn.quic.CloseWithError(0x0104, "")
                        }
                }()
        }
}

// readControlStream 读取服务端控制流：SETTINGS 设置并记录 QPACK 编码器参数，GOAWAY 后不再在本连接发起请求
func (conn *http3Conn) readControlStream(reader *bufio.Reader, encoder *qpackEncoder) error {
        for first := true; ; first = false {
                kind, payload, err := readH3Frame(reader)
                if err != nil {
                        return err
                }
                switch {
                case kind == h3FrameSettings && first:
                        capacity, blocked := uint64(0), uint64(0)
                        for len(payload) > 0 {
                                id, n, err := quicvarint.Parse(payload)
                                if err != nil {
                                        return err
                                }
                                value, m, err := quicvarint.Parse(payload[n:])
                                if err != nil {
                                        return err
                                }
                                payload = payload[n+m:]
                                switch id {
                                case h3SettingQPACKCapacity:
                                        capacity = value
                                case h3SettingQPACKBlocked:
                                        blocked = value
                                }
                        }
                        conn.remember(capacity, blocked)
                        conn.mu.Lock()
                        encoder.setPeerSettings(capacity, blocked)
                        err = conn.flushEncoderLocked()
                        conn.mu.Unlock()
                        if err != nil {
                                return err
                        }
                case first:
                        return fmt.Errorf("HTTP/3 控制流首帧不是 SETTINGS")
                case kind == h3FrameGoaway:
                        conn.goawayOnce.Do(func() { close(conn.goaway) })
                }
        }
}

func readH3Frame(reader *bufio.Reader) (uint64, []byte, error) {
        kind, err := quicvarint.Read(reader)
        if err != nil {
                return 0, nil, err
        }
        length, err := quicvarint.Read(reader)
        if err != nil {
                return 0, nil, err
        }
        if length > 16<<20 {
                return 0, nil, fmt.Errorf("HTTP/3 帧长度 %d 超出上限", length)
        }
        payload := make([]byte, length)
        _, err = io.ReadFull(reader, payload)
        return kind, payload, err
}

func (conn *http3Conn) close() {
        _ = conn.quic.CloseWithError(h3NoError, "")
}
