// Package atomicwrite 提供原子文件写入：先写同目录临时文件再重命名，
// 避免读者在写入过程中看到截断或半写状态。
package atomicwrite

import (
	"fmt"
	"os"
	"path/filepath"
)

// WriteFile 将 data 原子写入 filePath，mode 为目标文件权限。
func WriteFile(filePath string, data []byte, mode os.FileMode) error {
	target, err := filepath.Abs(filePath)
	if err != nil {
		return fmt.Errorf("解析文件路径: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return fmt.Errorf("创建文件目录: %w", err)
	}
	temporary, err := os.CreateTemp(filepath.Dir(target), "."+filepath.Base(target)+"-*.tmp")
	if err != nil {
		return fmt.Errorf("创建临时文件: %w", err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(mode); err != nil {
		temporary.Close()
		return fmt.Errorf("设置文件权限: %w", err)
	}
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return fmt.Errorf("写入临时文件: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return fmt.Errorf("同步临时文件: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("关闭临时文件: %w", err)
	}
	if err := os.Rename(temporaryPath, target); err != nil {
		return fmt.Errorf("替换文件: %w", err)
	}
	return nil
}
