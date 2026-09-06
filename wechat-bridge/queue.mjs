/** wechat-bridge/queue.mjs — TurnQueue（串行化 agent 调用，同一 agent 会话禁并发 turn）。
 * 从 bridge.mjs 纯搬运，零内部依赖。
 *
 *  run(task, opts?)
 *   - 无 opts（回复侧 askAgent/命令/轮换等）行为同旧版：严格 FIFO 串行，不限时。
 *   - opts = { deadline, waitMaxMs }（仅发送侧 /agent/prompt mode=send 使用，R10/F-A17-004）：
 *     给 turn 加排队预算——若在 waitMaxMs（或 deadline 余量）内仍未能开始处理 →
 *     快速判败 `queue_busy` 且**被取消的 turn 绝不执行**（不留孤儿 LLM 卡在队列里）。
 *     deadline 为整体超时点（覆盖排队+处理），由调用方在 wrap 处理步内兜底。 */
export class TurnQueue {
  constructor(maxQueue = 10) {
    this.tail = Promise.resolve()
    this.maxQueue = maxQueue
    this.pending = 0
  }

  run(task, opts = {}) {
    const { deadline, waitMaxMs } = opts
    const budgeted = deadline !== undefined || waitMaxMs !== undefined
    // 显式限界：pending（含正在执行的 1 个）>= maxQueue → 直接 queue_busy 拒绝，不入队
    if (this.pending >= this.maxQueue) {
      const err = new Error('queue_busy: TurnQueue 已满，拒绝新任务')
      err.code = 'QUEUE_BUSY'
      return Promise.reject(err)
    }
    this.pending++
    const dec = () => { this.pending = Math.max(0, this.pending - 1) }
    if (!budgeted) {
      const next = this.tail.then(task, task)
      this.tail = next.catch(() => {})
      return next.finally(dec)
    }
    // 预算版：gate 在前一 turn 结束后才放行任务；但若等待超过排队预算，
    // 先判败 queue_busy 并取消任务（重入 gate 回调见 cancelled 即不再执行）。
    const waitCap = Math.min(
      waitMaxMs !== undefined ? Math.max(0, waitMaxMs) : Infinity,
      deadline !== undefined ? Math.max(0, deadline - Date.now()) : Infinity,
    )
    let cancelled = false
    const gate = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        cancelled = true
        const err = new Error('queue_busy: RPC send 排队等待超时，未在预算内开始处理')
        err.code = 'QUEUE_BUSY'
        reject(err)
      }, waitCap === Infinity ? 2 ** 31 - 1 : waitCap)
      this.tail.then(() => {
        clearTimeout(timer)
        if (cancelled) return   // 已判败，任务不得执行
        resolve()
      })
    })
    const next = gate.then(() => task(), (err) => { cancelled = true; throw err })
    this.tail = next.catch(() => {})
    return next.finally(dec)
  }
}
