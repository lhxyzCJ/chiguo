#!/usr/bin/env node
/**
 * home-dir — 统一 home 目录解析（#357 Bun home 解析统一）
 *
 * 背景：bun runtime 的 os.homedir() 启动时捕获/缓存 passwd home（恒返回 /root），
 * 不跟随运行期 process.env.HOME 改动；官方 node 的 os.homedir() 每次实时读 $HOME。
 * 测试注入 process.env.HOME=临时目录定位 ~/.pi 与 ~/.chiguo 时，bun 因解析差异
 * 找不到目录 → 备份/轮换断言失败（本机 node 为 Bun wrapper，GitHub Actions 官方 node 无此问题）。
 *
 * 统一解析：process.env.HOME || os.homedir() —— 与官方 node os.homedir() 的 POSIX
 * 文档语义一致（UNIX 优先 $HOME）；生产（$HOME == passwd home）行为等价，跨 runtime 稳定。
 * 所有裸用 os.homedir() 的调用方（command-detect/agent-rpc/bridge/session-rotate/
 * agent-run）一律复用本 helper，禁止再散落直调。
 */
import { homedir } from 'node:os'

/** 解析用户 home 目录：优先运行期 $HOME，缺失（含空串）回退 os.homedir()。 */
export function homeDir() {
  return process.env.HOME || homedir()
}
