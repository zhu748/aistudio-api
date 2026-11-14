// 认证 SOCKS5 本地桥接器（复现脚本，凭据参数化）
//
// 用途: Chromium 不支持 RFC1929 用户名密码认证的 SOCKS5 代理，
//       本桥接器监听本地免认证端口，向上游完成认证握手后透明 relay。
//
// 环境变量:
//   BRIDGE_LISTEN_PORT   本地监听端口（默认 11080）
//   UPSTREAM_SOCKS_HOST  上游主机
//   UPSTREAM_SOCKS_PORT  上游端口
//   UPSTREAM_SOCKS_USER  上游用户名
//   UPSTREAM_SOCKS_PASS  上游密码
//
// 用法: node socks5_bridge.mjs   （配合 hybrid_capture.mjs 自动拉起）
import net from 'node:net';

const LISTEN_PORT = Number(process.env.BRIDGE_LISTEN_PORT || 11080);
const UPSTREAM_HOST = process.env.UPSTREAM_SOCKS_HOST || '127.0.0.1';
const UPSTREAM_PORT = Number(process.env.UPSTREAM_SOCKS_PORT || 1080);
const UPSTREAM_USER = process.env.UPSTREAM_SOCKS_USER || '';
const UPSTREAM_PASS = process.env.UPSTREAM_SOCKS_PASS || '';

if (!UPSTREAM_USER || !UPSTREAM_PASS) {
  console.error('missing UPSTREAM_SOCKS_USER / UPSTREAM_SOCKS_PASS');
  process.exit(1);
}

function socks5Greeting(methods) {
  // client: VER NMETHODS METHODS...
  return Buffer.from([0x05, 0x01, ...methods]);
}

function readSocksReply(buf, expectAuth = false) {
  if (expectAuth) return buf[0] === 0x01 && buf[1] === 0x00;
  return buf[0] === 0x05 && buf[1] === 0x00;
}

const server = net.createServer(client => {
  let stage = 'greeting';
  let header = null;

  const upstream = net.connect(UPSTREAM_PORT, UPSTREAM_HOST);
  upstream.setTimeout(30000);
  client.setTimeout(120000);

  const fail = () => { client.destroy(); upstream.destroy(); };
  upstream.on('error', fail);
  upstream.on('timeout', fail);
  client.on('error', fail);
  client.on('timeout', fail);

  // --- 上游握手状态机 ---
  upstream.on('data', chunk => {
    if (stage === 'upstream_greeting') {
      // expect 05 02 (用户名密码认证)
      if (!(chunk[0] === 0x05 && chunk[1] === 0x02)) return fail();
      const u = Buffer.from(UPSTREAM_USER, 'utf8');
      const p = Buffer.from(UPSTREAM_PASS, 'utf8');
      const authReq = Buffer.concat([
        Buffer.from([0x01, u.length]), u,
        Buffer.from([p.length]), p,
      ]);
      upstream.write(authReq);
      stage = 'upstream_auth';
    } else if (stage === 'upstream_auth') {
      if (!readSocksReply(chunk, true)) return fail();
      // 转发原始 CONNECT 请求
      upstream.write(header);
      stage = 'upstream_connect';
    } else if (stage === 'upstream_connect') {
      if (!(chunk[0] === 0x05 && chunk[1] === 0x00)) {
        console.error('upstream CONNECT failed, code', chunk[1]);
        return fail();
      }
      // 回复客户端: 05 00 00 01 00000000 0000
      client.write(Buffer.from([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]));
      stage = 'relay';
      client.pipe(upstream);
      upstream.pipe(client);
    }
  });

  // --- 客户端握手 ---
  client.on('data', chunk => {
    if (stage === 'greeting') {
      // 解析 greeting, 回复 NO AUTH
      client.write(Buffer.from([0x05, 0x00]));
      stage = 'client_request';
    } else if (stage === 'client_request') {
      header = chunk; // 原样保存 CONNECT 请求转发给上游
      // 启动上游握手
      stage = 'upstream_greeting';
      upstream.write(socks5Greeting([0x02]));
    }
    // relay 阶段由 pipe 处理
  });

  client.on('close', () => upstream.destroy());
  upstream.on('close', () => client.destroy());
});

server.listen(LISTEN_PORT, '127.0.0.1', () => {
  console.log(`socks5 bridge listening on 127.0.0.1:${LISTEN_PORT}`);
});
