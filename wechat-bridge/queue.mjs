export class TurnQueue {
  constructor(maxQueue = 10) {
    this.tail = Promise.resolve()
    this.maxQueue = maxQueue
    this.pending = 0
  }
  run(task, opts = {}) {
    const { deadline, waitMaxMs } = opts
    const budgeted = deadline !== undefined || waitMaxMs !== undefined
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
        if (cancelled) return
        resolve()
      })
    })
    const next = gate.then(() => task(), (err) => { cancelled = true; throw err })
    this.tail = next.catch(() => {})
    return next.finally(dec)
  }
}
