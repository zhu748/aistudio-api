package main

import (
	"flag"
	"testing"
)

func TestLocalListenOverridesPlatformPort(t *testing.T) {
	// 平台 PORT 为主服务生成公网默认值，但子进程的 -listen 必须优先。
	flags := flag.NewFlagSet("signer", flag.ContinueOnError)
	listen := flags.String("listen", "0.0.0.0:10000", "")
	secret := flags.String("shared-secret", "", "")
	if err := flags.Parse([]string{"-listen", "127.0.0.1:8081"}); err != nil {
		t.Fatal(err)
	}
	if err := validateListenAddress(*listen, *secret); err != nil {
		t.Fatal(err)
	}
}

func TestValidateListenAddress(t *testing.T) {
	for _, tc := range []struct {
		addr, secret string
		wantError    bool
	}{
		{"127.0.0.1:8081", "", false},
		{"[::1]:8081", "", false},
		{"localhost:8081", "", false},
		{"0.0.0.0:10000", "", true},
		{":8081", "", true},
		{"[::]:8081", "", true},
		{"192.168.1.2:8081", "", true},
		{"0.0.0.0:10000", "  ", true},
		{"0.0.0.0:10000", "secret-from-flag", false},
		{"invalid", "secret", true},
	} {
		t.Run(tc.addr+"/"+tc.secret, func(t *testing.T) {
			err := validateListenAddress(tc.addr, tc.secret)
			if (err != nil) != tc.wantError {
				t.Fatalf("validateListenAddress(%q): %v", tc.addr, err)
			}
		})
	}
}
