// turnfixture is a minimal Pion TURN server for the Python-side E2E test
// (tests/unit/test_proxy_bridge_e2e.py). It is NOT part of the bridge binary:
// the "testdata" directory is ignored by `go build .` / `go vet ./...`, and is
// compiled on demand by the test itself via `go build -o <tmp> testdata/turnfixture.go`.
//
// Behaviour: listens on an OS-assigned loopback port with long-term credentials
// (fixture-user / fixture-password, realm "fixture", RFC 6062 TCP relay),
// prints one JSON line {"turn_port == 0} to stdout once the server is ready,
// and keeps serving until SIGTERM/SIGINT.
package main

import (
	"encoding/json"
	"net"
	"os"
	"os/signal"
	"syscall"

	"github.com/pion/logging"
	"github.com/pion/transport/v5/stdnet"
	"github.com/pion/turn/v5"
)

const (
	fixtureUser     = "fixture-user"
	fixturePassword = "fixture-password"
	fixtureRealm    = "fixture"
)

func main() {
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		os.Stderr.WriteString("fixture_listen_failed\n")
		os.Exit(1)
	}
	logger := logging.NewDefaultLoggerFactory()
	logger.DefaultLogLevel = logging.LogLevelDisabled
	server, err := turn.NewServer(turn.ServerConfig{
		Realm:         fixtureRealm,
		LoggerFactory: logger,
		AuthHandler: func(r *turn.RequestAttributes) (string, []byte, bool) {
			return r.Username, turn.GenerateAuthKey(fixtureUser, fixtureRealm, fixturePassword), r.Username == fixtureUser
		},
		ListenerConfigs: []turn.ListenerConfig{{
			Listener: ln,
			RelayAddressGenerator: &turn.RelayAddressGeneratorStatic{
				RelayAddress: net.ParseIP("127.0.0.1"),
				Address:      "127.0.0.1",
				Net:          &stdnet.Net{},
			},
		}},
	})
	if err != nil {
		ln.Close()
		os.Stderr.WriteString("fixture_server_failed\n")
		os.Exit(1)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	if err := json.NewEncoder(os.Stdout).Encode(map[string]int{"turn_port": port}); err != nil {
		server.Close()
		os.Stderr.WriteString("fixture_stdout_failed\n")
		os.Exit(1)
	}
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGTERM, syscall.SIGINT)
	<-ch
	server.Close()
}
