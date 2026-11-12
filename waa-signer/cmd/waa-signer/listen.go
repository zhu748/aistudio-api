package main

import (
	"fmt"
	"net"
	"strings"
)

func validateListenAddress(addr, secret string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("无效的监听地址 %q: %w", addr, err)
	}
	ip := net.ParseIP(host)
	if host == "localhost" || (ip != nil && ip.IsLoopback()) || strings.TrimSpace(secret) != "" {
		return nil
	}
	return fmt.Errorf("监听地址 %q 对外开放，必须设置 SIGNER_SHARED_SECRET 或 -shared-secret；本机签名请使用 -listen 127.0.0.1:8081", addr)
}
