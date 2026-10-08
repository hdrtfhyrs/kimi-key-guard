// 纯判断逻辑，不碰浏览器 API，方便单独测试。
// 百分比都是 0~100 的数；时间都是毫秒时间戳。

export const LIFETIME_MS = 5 * 60 * 60 * 1000; // key 活 5 小时
export const PRE_RESET_MS = 90 * 1000;         // 刷新前 90 秒查一次
export const POST_RESET_MS = 30 * 1000;        // 刷新后 30 秒确认一次
export const MIN_GAP_MS = 30 * 1000;           // Chrome 闹钟最短 30 秒

// 第一次发现这个 key 时建档：当前用量就是起点
export function newTrack(apiKey, usage, now) {
  const createTime = Date.parse(apiKey.createTime);
  return {
    id: apiKey.id,
    name: apiKey.name,
    createTime,
    deadline: createTime + LIFETIME_MS,
    baseline: usage.used,   // 起点
    carry: 0,               // 之前各周期已用掉的，累加
    lastUsed: usage.used,
    lastReset: usage.resetTime,
    firstSeen: now,
  };
}

// 拿到一次用量后做判断。返回新状态、是否删除、下次什么时候查。
export function evaluate(track, usage, now) {
  const t = { ...track };
  const notes = [];

  // 重置时间往后跳才结转；用量小幅回调不当成新窗口。
  const resetHappened = usage.resetTime > t.lastReset + 60 * 1000;
  if (resetHappened) {
    const spent = Math.max(0, t.lastUsed - t.baseline);
    t.carry += spent;
    t.baseline = 0;
    notes.push(`检测到额度刷新，上个周期用了 ${spent}%，累计结转 ${t.carry}%`);
  }
  t.lastUsed = usage.used;
  t.lastReset = usage.resetTime;

  const consumed = t.carry + Math.max(0, usage.used - t.baseline);
  t.consumed = consumed;

  if (now >= t.deadline) {
    return { track: t, del: true, reason: '已满 5 小时', notes };
  }
  if (consumed >= 100) {
    return { track: t, del: true, reason: `累计用量 ${consumed}% 已到 100%`, notes };
  }

  // 到期、窗口重置前后与持续轮询取最早时间。
  const points = [t.deadline];
  if (usage.resetTime < t.deadline) {
    const pre = usage.resetTime - PRE_RESET_MS;
    points.push(now < pre ? pre : usage.resetTime + POST_RESET_MS);
  }
  // 从第一窗口就持续取数；平台限流不能替代本程序先取证再撤销。
  points.push(now + (consumed >= 80 ? MIN_GAP_MS : 60 * 1000));

  const nextCheck = Math.max(now + MIN_GAP_MS, Math.min(...points));
  return { track: t, del: false, nextCheck, notes };
}
