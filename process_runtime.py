"""D-drive authoritative multi-process guard. No credentials or browser execution.

Only supervise/worker CLI modes run loops. Native messaging calls handle() using
short SQLite WAL transactions; each key's decisions belong to its own process.
"""
import contextlib
import ctypes
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
sys.dont_write_bytecode = True
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION = '2.0.2'
HERE = Path(__file__).resolve().parent
from guard_paths import DATA_ROOT
ROOT = DATA_ROOT
STORE = ROOT / 'process-runtime'
DB = STORE / 'state.sqlite3'
FIVE_HOURS = 18000000
JOB_TTL = 90000
STALE_HEARTBEAT = 45000
TERMINAL = {'completed', 'expired', 'cancelled'}
INVALID_STATUSES = {'STATUS_DISABLED', 'STATUS_REVOKED', 'STATUS_DELETED'}


def now():
    return int(time.time() * 1000)


def checked_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ValueError('精确API ID无效')
    return value


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def millis(value):
    if number(value):
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return int(parsed.timestamp() * 1000)
        except ValueError:
            pass
    return None


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat() if number(value) else None


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            stream.write(dump(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextlib.contextmanager
def transaction():
    STORE.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB), timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA busy_timeout=2000')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, target TEXT NOT NULL, kind TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL, result TEXT, handled INTEGER NOT NULL DEFAULT 0, command TEXT NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS jobs_target ON jobs(target,status)')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('paused', 'true'))
        yield connection
        connection.execute('COMMIT')
    except Exception:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()


def meta(db, name, default=None):
    row = db.execute('SELECT value FROM meta WHERE name=?', (name,)).fetchone()
    return json.loads(row['value']) if row else default


def setmeta(db, name, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (name, dump(value)))


def tracks(db):
    return {row['id']: json.loads(row['value']) for row in db.execute('SELECT * FROM tracks')}


def gettrack(db, target):
    row = db.execute('SELECT value FROM tracks WHERE id=?', (target,)).fetchone()
    return json.loads(row['value']) if row else None


def save(db, track):
    db.execute('INSERT OR REPLACE INTO tracks VALUES (?,?)', (track['id'], dump(track)))


class Mutex:
    """Lifetime Windows mutex; named per exact key, abandoned owners recover."""
    def __init__(self, name, timeout=0):
        if os.name != 'nt':
            raise RuntimeError('正式多进程入口要求Windows命名mutex')
        from ctypes import wintypes
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateMutexW(None, False, 'Local\\KimiGuard2-' + name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = self.api.WaitForSingleObject(self.handle, timeout)
        self.owned = result in (0, 0x80)
        if result == 0xFFFFFFFF:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if getattr(self, 'handle', None):
            if getattr(self, 'owned', False):
                self.api.ReleaseMutex(self.handle)
            self.api.CloseHandle(self.handle)
            self.handle = None


def process_handle(pid, access):
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    handle = api.OpenProcess(access, False, int(pid))
    return api, handle


def identity(pid):
    if not pid or os.name != 'nt':
        return None
    from ctypes import wintypes
    api, handle = process_handle(pid, 0x1000)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        exit_code = wintypes.DWORD()
        if not api.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            return None
        if not api.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        api.CloseHandle(handle)


def terminate_exact(pid, created):
    """Compare creation time on the SAME opened handle before termination."""
    from ctypes import wintypes
    api, handle = process_handle(pid, 0x1001)
    if not handle:
        return False
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(item) for item in values)):
            return False
        current = str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        return current == created and bool(api.TerminateProcess(handle, 1))
    finally:
        api.CloseHandle(handle)


def dossier(target):
    return ROOT / 'leases' / checked_id(target)


def export(target):
    lock = Mutex('export-' + target, 2000)
    try:
        if not lock.owned:
            return
        with transaction() as db:
            track = gettrack(db, target)
        if track:
            atomic_json(dossier(target) / '进程状态.json', track)
    finally:
        lock.close()


def event(target, name, detail=None):
    directory = dossier(target)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '进程日志.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(dump({'at': now(), 'event': name, 'detail': detail}) + '\n')


def newtrack(key, observed):
    return {'id': checked_id(key['id']), 'name': '5小时', 'createTime': millis(key.get('createTime')),
            'sentAt': None, 'deadline': None, 'baseline': None, 'startBaseline': None,
            'lastUsed': None, 'lastReset': None, 'reset': None, 'carry': None, 'consumed': None,
            'done': False, 'phase': 'awaiting_delivery', 'nextCheck': observed,
            'pid': None, 'processCreated': None, 'heartbeat': None, 'epoch': 1,
            'lastError': None, 'quotaReliable': False, 'firstSeen': observed,
            'discoverySource': 'Chrome完整ListAPIKeys自动发现', 'startSource': None,
            'quotaStatus': '实际发出与起点待核', 'paused': False, 'live': True,
            'lastListAt': observed, 'needsReconcile': True, 'exclusiveUsage': False}


def eligible(key):
    return isinstance(key, dict) and key.get('name') == '5小时' and key.get('status') not in INVALID_STATUSES


def close_jobs(db, target):
    db.execute("UPDATE jobs SET status='cancelled' WHERE target=? AND status IN ('pending','dispatched')", (target,))


def sync(db, keys, observed, known=None):
    if not isinstance(keys, list) or any(not isinstance(key, dict) or not key.get('id') for key in keys):
        raise ValueError('未取得完整API列表，禁止归档或发现')
    if observed < (meta(db, 'lastListAt', 0) or 0):
        return
    current = tracks(db)
    live = {key['id']: key for key in keys if eligible(key)}
    for target, key in live.items():
        checked_id(target)
        track = current.get(target)
        if track is None:
            track = newtrack(key, observed)
            prior = (known or {}).get(target)
            if isinstance(prior, dict):
                # Explicit Chrome actual-delivery provenance only; no old seed inference.
                if prior.get('sentAt') is not None and prior.get('startSource') and not prior.get('done'):
                    sent = millis(prior['sentAt'])
                    if sent is not None:
                        for field in ('baseline', 'startBaseline', 'carry', 'lastUsed', 'lastReset', 'consumed', 'lastUsageAt'):
                            track[field] = prior.get(field)
                        if not number(track.get('startBaseline')):
                            track['startBaseline'] = prior.get('baseline')
                        track['deliveryCarry'] = prior.get('carry')
                        track.update(sentAt=sent, deadline=sent + FIVE_HOURS, startSource=prior['startSource'], phase='active',
                                     exclusiveUsage=prior.get('exclusiveUsage') is True, migrationSource='Chrome已确认实际发出记录')
                if prior.get('done') is True:
                    # A platform-live ID contradicts an ended cache. Observe it
                    # independently; do not inherit that cache's quota or clock.
                    track.update(lastError='Chrome记录结束但平台ID仍有效，实际交付待核', migrationSource='Chrome结束状态与平台有效列表冲突')
            current[target] = track
        if observed >= (track.get('lastListAt') or 0):
            track.update(live=True, lastListAt=observed, platformStatus=key.get('status'))
        save(db, track)
    for target, track in current.items():
        if observed < (track.get('lastListAt') or 0):
            continue
        if target not in live:
            track.update(live=False, lastListAt=observed)
            if not track['done']:
                track.update(done=True, phase='ended', endedAt=observed,
                             doneReason='目标已不在完整有效列表；未由此冒称删除响应成功')
                track['epoch'] += 1
                close_jobs(db, target)
        if len(live) != 1:
            track.update(quotaReliable=False, quotaContinuityLost=True, quotaStatus='多个有效服务key或无有效key，共享额度归属未知')
        save(db, track)
    setmeta(db, 'lastListAt', observed)
    setmeta(db, 'liveServiceIds', list(live))
    # Migration: preserve ended absent Chrome records, never resurrect them.
    for target, prior in (known or {}).items():
        if target in current or target in live or not isinstance(prior, dict) or prior.get('done') is not True:
            continue
        checked_id(target)
        track = newtrack({'id': target}, observed)
        track.update(done=True, live=False, phase='ended', doneReason=prior.get('doneReason', '迁移Chrome历史结束记录'), migrationSource='Chrome历史结束记录')
        save(db, track)


def lease_record(track):
    return {'id': track['id'], 'name': '5小时', 'sentAt': iso(track['sentAt']),
            'baseline': track.get('startBaseline'), 'carry': track.get('deliveryCarry'),
            'source': track.get('startSource'), 'registeredAt': iso(now())}


def ensure_lease(track):
    if not number(track.get('sentAt')):
        return
    path = dossier(track['id']) / '租期.json'
    value = lease_record(track)
    lock = Mutex('lease-' + track['id'], 2000)
    try:
        if not lock.owned:
            raise ValueError('本单租期写入暂时忙，保留原件')
        if path.exists():
            old = json.loads(path.read_text(encoding='utf-8'))
            if millis(old.get('sentAt')) != track['sentAt'] or old.get('baseline') != value['baseline']:
                raise ValueError('本单租期原件不同，保留原件不覆盖')
        else:
            atomic_json(path, value)
    finally:
        lock.close()


def delivery(db, source):
    if not isinstance(source, dict) or source.get('name', '5小时') != '5小时':
        raise ValueError('交付记录无效')
    target = checked_id(source.get('id'))
    sent = millis(source.get('sentAt'))
    start = source.get('startBaseline', source.get('baseline'))
    carry = source.get('carry')
    if sent is None or sent > now() + 60000:
        raise ValueError('实际发出时间无效，不能用创建或发现时间代填')
    if start is not None and (not number(start) or not 0 <= start <= 100):
        raise ValueError('实际用量起点无效')
    if carry is not None and (not number(carry) or carry < 0):
        raise ValueError('本单结转无效')
    track = gettrack(db, target) or newtrack(source, now())
    if number(track.get('sentAt')):
        if (track['sentAt'], track.get('startBaseline'), track.get('deliveryCarry')) != (sent, start, carry):
            raise ValueError('本单已登记且参数不同，保留原件')
        return track
    if track.get('done'):
        raise ValueError('本单已结束，不重新发起守卫')
    created = track.get('createTime') or millis(source.get('createTime'))
    if number(created) and sent < created - 60000:
        raise ValueError('实际发出不能早于创建')
    track.update(sentAt=sent, deadline=sent + FIVE_HOURS, startBaseline=start,
                 baseline=source.get('baseline', start), carry=carry, deliveryCarry=carry,
                 lastUsed=source.get('lastUsed'), lastReset=source.get('lastReset'),
                 consumed=None, phase='active', nextCheck=now(), needsReconcile=True,
                 startSource=source.get('startSource') or source.get('source') or '本人确认实际已发出',
                 exclusiveUsage=source.get('exclusiveUsage') is True)
    track['epoch'] += 1
    close_jobs(db, target)
    ensure_lease(track)
    save(db, track)
    return track


def status(db):
    all_tracks = tracks(db)
    return {'ok': True, 'paused': meta(db, 'paused', True), 'tracks': all_tracks,
            'workers': {key: {field: track.get(field) for field in ('pid', 'heartbeat', 'phase', 'processCreated')}
                        | {'state': track.get('phase')} for key, track in all_tracks.items()},
            'supervisor': meta(db, 'supervisor', {'pid': None, 'heartbeat': None}),
            'bridgeHeartbeat': meta(db, 'bridgeHeartbeat'), 'runtimeVersion': VERSION,
            'jobs': [{key: row[key] for key in ('id', 'target', 'kind', 'epoch', 'status', 'created', 'expires')}
                     | {'jobId': row['id']} for row in db.execute("SELECT * FROM jobs WHERE status IN ('pending','dispatched') ORDER BY created")]}


def expire(db, at):
    for job in db.execute("SELECT * FROM jobs WHERE status IN ('pending','dispatched') AND expires<=?", (at,)).fetchall():
        db.execute("UPDATE jobs SET status='expired' WHERE id=?", (job['id'],))
        track = gettrack(db, job['target'])
        if track and not track['done']:
            track.update(needsReconcile=True, nextCheck=at + 5000, lastError='浏览器任务超时，先核完整列表再决定重试')
            if track.get('manualRevoke', {}).get('status') == 'pending':
                track['manualRevoke']['status'] = 'expired'
            save(db, track)


def reason(track, at):
    if number(track.get('sentAt')) and number(track.get('deadline')) and at >= track['deadline']:
        return '已满5小时'
    if track.get('quotaReliable') is True and number(track.get('consumed')) and track['consumed'] >= 100:
        return '本单可靠累计用满100%'
    return None


def manual_job(db, target, job_id, epoch, request_id):
    track = gettrack(db, checked_id(target))
    job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    request = (track or {}).get('manualRevoke') or {}
    if not track or track['done'] or track.get('live') is not True:
        raise ValueError('手动目标已结束或不在有效列表')
    if not job or job['target'] != target or job['kind'] != 'revoke' or job['status'] != 'dispatched' or job['expires'] <= now():
        raise ValueError('手动撤销任务已失效')
    if job['epoch'] != track['epoch'] or epoch != track['epoch']:
        raise ValueError('本ID的手动请求已取消或代次改变')
    if request.get('requestId') != request_id or request.get('status') != 'pending' or request.get('expiresAt', 0) <= now():
        raise ValueError('本人手动确认已失效，需要重新点击确认')
    if json.loads(job['command']).get('manualRequestId') != request_id:
        raise ValueError('手动确认不属于这份任务')
    return track, job, request


def enqueue_manual(db, track, request):
    at = now()
    if request.get('status') != 'pending' or request.get('expiresAt', 0) <= at:
        return
    command = {'jobId': uuid.uuid4().hex, 'id': track['id'], 'kind': 'revoke', 'epoch': track['epoch'],
               'expiresAt': request['expiresAt'], 'manualRequestId': request['requestId'], 'track': track}
    db.execute('INSERT INTO jobs(id,target,kind,epoch,status,created,expires,command) VALUES (?,?,?,?,?,?,?,?)',
               (command['jobId'], track['id'], 'revoke', track['epoch'], 'pending', at, command['expiresAt'], dump(command)))


def request_manual(db, message):
    target = checked_id(message.get('id'))
    request_id = checked_id(message.get('confirmationId'))
    if message.get('confirmed') is not True:
        raise ValueError('尚未确认这枚精确ID的真实删除')
    track = gettrack(db, target)
    if not track or track['done'] or track.get('live') is not True or track.get('name') != '5小时':
        raise ValueError('所选key已不在当前有效服务列表')
    previous = track.get('manualRevoke') or {}
    if previous.get('status') == 'pending' and previous.get('expiresAt', 0) > now():
        return track
    if track.get('revokeIntentAt'):
        pending = db.execute("SELECT 1 FROM jobs WHERE target=? AND status IN ('pending','dispatched') AND expires>?", (target, now())).fetchone()
        if pending or (track.get('lastListAt') or 0) < track['revokeIntentAt']:
            raise ValueError('本ID撤销结果待确认，先核完整列表，不重复发新的删除')
        attempts = track.setdefault('previousRevokeAttempts', [])
        attempts.append({'intentAt': track['revokeIntentAt'], 'beforeEvidence': track.get('beforeEvidence'), 'confirmedStillPresentAt': track.get('lastListAt')})
        track['revokeIntentAt'] = None
    at = now()
    track['epoch'] += 1
    close_jobs(db, target)
    track['manualRevoke'] = {'requestId': request_id, 'requestedAt': at, 'expiresAt': at + JOB_TTL,
                             'status': 'pending', 'source': '本人在弹窗确认精确ID立即撤销'}
    track.update(lastError=None, phase='awaiting_evidence')
    enqueue_manual(db, track, track['manualRevoke'])
    save(db, track)
    return track


def record_revoke_result(db, message):
    target = checked_id(message.get('id'))
    track = gettrack(db, target)
    evidence_id = message.get('evidenceId', '')
    if not track or not re.fullmatch(r'[a-f0-9]{32}', str(evidence_id)) or (track.get('beforeEvidence') or {}).get('evidenceId') != evidence_id:
        raise ValueError('完成回执与本ID已授权证据不符')
    directory = ROOT / 'screenshots' / target / evidence_id
    before = json.loads((directory / '删除前记录.json').read_text(encoding='utf-8'))
    result = json.loads((directory / '删除结果.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256((directory / '删除前真实页面.png').read_bytes()).hexdigest()
    if result.get('targetId') != target or result.get('confirmedAbsent') is not True or digest != before.get('sha256') or digest != result.get('beforeScreenshotSha256'):
        raise ValueError('完成回执或原始PNG校验失败')
    track.update(done=True, live=False, phase='ended', endedAt=millis(result.get('confirmedAbsentAt')) or now(),
                 deletedAt=millis(result.get('deletedAt')), responseConfirmed=result.get('deleteResponseConfirmed') is True,
                 doneReason=result.get('reason'), lastError=None,
                 resultEvidence={'evidenceId': evidence_id, 'screenshotPath': str(directory / '删除前真实页面.png'),
                                 'resultPath': str(directory / '删除结果.json'), 'sha256': digest})
    request = track.get('manualRevoke')
    if request and before.get('manualRequestId') == request.get('requestId'):
        request.update(status='completed', completedAt=track['endedAt'])
    track['epoch'] += 1
    close_jobs(db, target)
    save(db, track)
    return track


def authorize(db, message):
    target = checked_id(message.get('id'))
    track = gettrack(db, target)
    job = db.execute('SELECT * FROM jobs WHERE id=?', (message.get('jobId'),)).fetchone()
    at = now()
    is_manual = bool(message.get('manualRequestId'))
    if is_manual:
        track, job, manual = manual_job(db, target, message.get('jobId'), message.get('epoch'), message['manualRequestId'])
    if not track or track['done'] or not track.get('live') or (not is_manual and (track.get('paused') or meta(db, 'paused', True))):
        raise ValueError('本单结束、失效或已暂停，禁止删除')
    if not job or job['target'] != target or job['kind'] != 'revoke' or job['status'] != 'dispatched' or job['expires'] <= at:
        raise ValueError('撤销任务已失效，禁止删除')
    if job['epoch'] != track['epoch'] or message.get('epoch') != track['epoch']:
        raise ValueError('本单任务代次失效，禁止删除')
    evidence_id = message.get('evidenceId', '')
    if not isinstance(evidence_id, str) or not re.fullmatch(r'[a-f0-9]{32}', evidence_id):
        raise ValueError('删除前证据ID无效')
    directory = ROOT / 'screenshots' / target / evidence_id
    record = json.loads((directory / '删除前记录.json').read_text(encoding='utf-8'))
    lease = None if is_manual else json.loads((dossier(target) / '租期.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256((directory / '删除前真实页面.png').read_bytes()).hexdigest()
    snapshot = message.get('snapshot')
    if not isinstance(snapshot, dict) or snapshot.get('id') != target:
        raise ValueError('本单最新快照ID不符')
    if record.get('purpose') != 'before_deletion' or record.get('targetId') != target or digest != record.get('sha256') or digest != message.get('sha256'):
        raise ValueError('本单真实PNG落盘SHA校验失败，禁止删除')
    if millis(record.get('storedAt')) is None or millis(record['storedAt']) < job['created']:
        raise ValueError('删除前图早于本次撤销任务，禁止复用旧证据')
    if millis(record.get('page', {}).get('refreshedAt')) is None or millis(record['page']['refreshedAt']) < job['created']:
        raise ValueError('本次未刷新真实页面，禁止删除')
    if record.get('page', {}).get('targetPresent') is not True:
        raise ValueError('刷新后未见本单精确ID，禁止删除')
    for snap in (snapshot, record.get('snapshot', {})):
        if millis(snap.get('sentAt')) != track['sentAt'] or millis(snap.get('deadline')) != track['deadline']:
            raise ValueError('截图租期与D盘登记不符，禁止删除')
        if snap.get('baseline') != track.get('startBaseline'):
            raise ValueError('截图起点与本单实际登记不符')
    if is_manual and (record.get('manualRequestId') != message['manualRequestId'] or record.get('jobId') != job['id']):
        raise ValueError('真实截图不属于本次手动确认')
    if not is_manual and (millis(lease.get('sentAt')) != track['sentAt'] or lease.get('baseline') != track.get('startBaseline')):
        raise ValueError('租期原件不一致，禁止删除')
    why = '本人确认立即撤销，验证真实截图与删除链' if is_manual else reason(track, at)
    if not why:
        raise ValueError('未达到本单可靠停止条件，禁止删除')
    # The independent actor must already establish the stop condition. A later
    # fresh read can increase above 100%, but must not fall below it.
    if not is_manual and why != '已满5小时' and (snapshot.get('quotaReliable') is not True or not number(snapshot.get('consumed')) or snapshot['consumed'] < 100):
        raise ValueError('最新用量快照与守卫累计不符，禁止删除')
    track.update(revokeIntentAt=at, beforeEvidence={'evidenceId': evidence_id, 'sha256': digest,
                 'jobId': job['id'], 'manualRequestId': message.get('manualRequestId'),
                 'screenshotPath': str(directory / '删除前真实页面.png')}, phase='revoking', revokeReason=why)
    save(db, track)
    return {'ok': True, 'permit': True, 'jobId': job['id'], 'id': target, 'epoch': track['epoch'], 'expiresAt': job['expires'], 'reason': why}


def handle(message):
    try:
        action = message.get('action')
        changed = []
        with transaction() as db:
            if action == 'process_restore':
                records = message.get('records')
                if not isinstance(records, list):
                    raise ValueError('历史恢复须为记录数组')
                for record in records:
                    key = record.get('key') or {}
                    target = checked_id(key.get('id'))
                    observed = millis(record.get('observedAt'))
                    if not eligible(key) or observed is None or observed > now():
                        raise ValueError('历史恢复的ID、来源时刻无效')
                    if gettrack(db, target) is not None:
                        continue
                    track = newtrack(key, observed)
                    track.update(live=None, needsReconcile=True, nextCheck=now(),
                                 discoverySource='恢复历史真实页面观测，当前存在性待核',
                                 restoreSource=record.get('source'), quotaContinuityLost=True)
                    save(db, track)
                    changed.append(target)
            elif action == 'process_sync':
                if message.get('completeList', True) is not True:
                    raise ValueError('不完整列表不能同步归档')
                sync(db, message.get('keys'), millis(message.get('observedAt')) or now(), message.get('knownTracks'))
                changed = list(tracks(db))
            elif action == 'process_delivery':
                changed = [delivery(db, message.get('track'))['id']]
            elif action == 'process_manual_revoke':
                changed = [request_manual(db, message)['id']]
            elif action == 'process_manual_evidence':
                track, job, request = manual_job(db, message.get('id'), message.get('jobId'), message.get('epoch'), message.get('manualRequestId'))
                answer = {'ok': True, 'track': track, 'manualRequest': request}
            elif action == 'process_record_result':
                changed = [record_revoke_result(db, message)['id']]
            elif action == 'process_control':
                if not isinstance(message.get('paused'), bool):
                    raise ValueError('paused须为布尔值')
                target = message.get('id')
                if target:
                    checked_id(target)
                    track = gettrack(db, target)
                    if track is None:
                        raise ValueError('本单未登记')
                    track.update(paused=message['paused'], epoch=track['epoch'] + 1, needsReconcile=True, nextCheck=now())
                    if track.get('manualRevoke', {}).get('status') == 'pending':
                        track['manualRevoke']['status'] = 'cancelled'
                    close_jobs(db, target)
                    save(db, track)
                    changed = [target]
                else:
                    setmeta(db, 'paused', message['paused'])
                    for target, track in tracks(db).items():
                        track.update(epoch=track['epoch'] + 1, needsReconcile=True, nextCheck=now())
                        if track.get('manualRevoke', {}).get('status') == 'pending':
                            track['manualRevoke']['status'] = 'cancelled'
                        close_jobs(db, target)
                        save(db, track)
                        changed.append(target)
            elif action == 'process_exchange':
                setmeta(db, 'bridgeHeartbeat', now())
                expire(db, now())
                results = message.get('results') or []
                if not isinstance(results, list):
                    raise ValueError('results须为数组')
                for entry in results:
                    job = db.execute('SELECT * FROM jobs WHERE id=?', (entry.get('jobId'),)).fetchone()
                    if not job or job['status'] != 'dispatched':
                        continue
                    track = gettrack(db, job['target'])
                    if not track or track['epoch'] != job['epoch']:
                        continue
                    db.execute("UPDATE jobs SET status='completed', result=? WHERE id=?", (dump(entry), job['id']))
                    result = entry.get('result') or {}
                    if json.loads(job['command']).get('manualRequestId') and entry.get('ok') is not True:
                        request = track.get('manualRevoke') or {}
                        request.update(status='failed', error=str(entry.get('error') or '手动撤销失败'))
                        track['manualRevoke'] = request
                        save(db, track)
                    if entry.get('ok') is True and job['kind'] == 'observe' and result.get('completeList') is True:
                        sync(db, result.get('keys'), millis(result.get('observedAt')) or now())
                        changed = list(tracks(db))
            elif action == 'process_authorize':
                answer = authorize(db, message)
                changed = [message['id']]
            elif action != 'process_status':
                raise ValueError('不支持的进程动作')
            if action not in {'process_authorize', 'process_manual_evidence'}:
                answer = status(db)
            if action == 'process_exchange':
                commands = []
                for row in db.execute("SELECT * FROM jobs WHERE status IN ('pending','dispatched') AND expires>? ORDER BY created", (now(),)).fetchall():
                    track = gettrack(db, row['target'])
                    if not track or track['done'] or row['epoch'] != track['epoch']:
                        db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (row['id'],))
                        continue
                    command = json.loads(row['command'])
                    if row['kind'] == 'revoke' and not command.get('manualRequestId') and (meta(db, 'paused', True) or track.get('paused')):
                        continue
                    db.execute("UPDATE jobs SET status='dispatched' WHERE id=?", (row['id'],))
                    command['track'] = track
                    commands.append(command)
                answer = status(db) | {'commands': commands}
        for target in changed:
            export(target)
        return answer
    except Exception as error:
        return {'ok': False, 'error': str(error)}


def apply_usage(db, track, result):
    usage = result.get('usage')
    if not isinstance(usage, dict) or not number(usage.get('used')) or not number(usage.get('resetTime')):
        if reason(track, now()) == '已满5小时':
            track['quotaStatus'] = '按实际交付时间已到期；历史累计不倒推，删除前仍截真实页面'
            return
        track['lastError'] = result.get('usageError') or '共享额度读数未取得；本单时间继续'
        return
    used, reset = float(usage['used']), int(usage['resetTime'])
    if not 0 <= used <= 100:
        track.update(quotaReliable=False, lastError='共享额度读数越界')
        return
    observed = millis(result.get('usageObservedAt')) or millis(result.get('observedAt')) or now()
    previous_reset, previous_used = track.get('lastReset'), track.get('lastUsed')
    exclusive = meta(db, 'liveServiceIds', []) == [track['id']] and track.get('exclusiveUsage') is True
    ready = number(track.get('sentAt')) and number(track.get('baseline')) and number(track.get('carry'))
    reliable = exclusive and ready and not track.get('quotaContinuityLost')
    if reliable and not number(previous_reset):
        # A first sample from a later window cannot reconstruct missing history.
        if track['sentAt'] < reset - FIVE_HOURS - 60000:
            reliable = False
            track['quotaContinuityLost'] = True
    elif reliable and reset != previous_reset:
        # Observed maximum before a boundary is only a lower bound; retain that
        # lower bound, never invent 100%. Multiple missed windows lose continuity.
        if reset < previous_reset or reset - previous_reset > FIVE_HOURS + 60000 or not number(previous_used):
            reliable = False
            track['quotaContinuityLost'] = True
        else:
            track['carry'] += max(0, previous_used - track['baseline'])
            track['baseline'] = 0
    elif reliable and number(previous_used) and used + 0.5 < previous_used:
        reliable = False
        track['quotaContinuityLost'] = True
    track.update(lastUsed=used, lastReset=reset, reset=reset, lastUsageAt=observed, quotaReliable=reliable)
    if reliable:
        track['consumed'] = track['carry'] + max(0, used - track['baseline'])
        track['quotaStatus'] = '已确认独占条件下的累计观测下界'
    else:
        track['quotaStatus'] = '共享额度归属或历史窗口未知，不据此提前撤销'


def result_jobs(db, track):
    for job in db.execute("SELECT * FROM jobs WHERE target=? AND status='completed' AND handled=0 ORDER BY created", (track['id'],)).fetchall():
        db.execute('UPDATE jobs SET handled=1 WHERE id=?', (job['id'],))
        if job['epoch'] != track['epoch']:
            continue
        entry = json.loads(job['result'])
        result = entry.get('result') or {}
        if entry.get('ok') is not True:
            count = min(6, (track.get('failures') or 0) + 1)
            track.update(lastError=str(entry.get('error') or '浏览器动作未成功'), failures=count,
                         needsReconcile=True, nextCheck=now() + min(60000, 3000 * (2 ** count)))
            continue
        if job['kind'] == 'observe':
            if result.get('completeList') is not True:
                track.update(lastError='观察未返回完整列表', needsReconcile=True, nextCheck=now() + 10000)
                continue
            track.update(needsReconcile=False, failures=0, lastCheck=now(), lastError=None)
            apply_usage(db, track, result)
            track['nextCheck'] = now() + 15000
        elif result.get('confirmedAbsent') is True and track.get('revokeIntentAt'):
            track.update(done=True, live=False, phase='ended', endedAt=millis(result.get('confirmedAbsentAt')) or now(),
                         deletedAt=millis(result.get('deletedAt')), responseConfirmed=result.get('responseConfirmed') is True,
                         resultEvidence=result.get('evidence'), doneReason=result.get('reason') or track.get('revokeReason'))
            track['epoch'] += 1
            close_jobs(db, track['id'])
        else:
            track.update(needsReconcile=True, nextCheck=now() + 10000, lastError='撤销结果未取得完整列表消失确认，先核列表')


def issue(db, track, kind):
    if db.execute("SELECT 1 FROM jobs WHERE target=? AND status IN ('pending','dispatched')", (track['id'],)).fetchone():
        return
    created = now()
    command = {'jobId': uuid.uuid4().hex, 'id': track['id'], 'kind': kind, 'epoch': track['epoch'],
               'expiresAt': created + JOB_TTL, 'track': track}
    db.execute('INSERT INTO jobs(id,target,kind,epoch,status,created,expires,command) VALUES (?,?,?,?,?,?,?,?)',
               (command['jobId'], track['id'], kind, track['epoch'], 'pending', created, command['expiresAt'], dump(command)))
    track['phase'] = 'awaiting_evidence' if kind == 'revoke' else ('active' if track.get('sentAt') else 'awaiting_delivery')


def worker(target):
    target = checked_id(target)
    lock = Mutex('worker-' + target)
    if not lock.owned:
        lock.close()
        return
    pid, created = os.getpid(), identity(os.getpid())
    event(target, 'worker_started', {'pid': pid, 'processCreated': created})
    try:
        with transaction() as db:
            track = gettrack(db, target)
            if track and not track['done']:
                track.update(epoch=track['epoch'] + 1, needsReconcile=True, nextCheck=now(),
                             pid=pid, processCreated=created, heartbeat=now())
                close_jobs(db, target)
                if track.get('manualRevoke'):
                    enqueue_manual(db, track, track['manualRevoke'])
                save(db, track)
        while True:
            with transaction() as db:
                track = gettrack(db, target)
                if not track or track['done']:
                    break
                expire(db, now())
                track = gettrack(db, target)
                result_jobs(db, track)
                track.update(pid=pid, processCreated=created, heartbeat=now())
                if not track['done']:
                    due = reason(track, now())
                    track['stopReason'] = due
                    enabled = not meta(db, 'paused', True) and not track.get('paused')
                    if track.get('needsReconcile') and now() >= (track.get('nextCheck') or 0):
                        issue(db, track, 'observe')
                    elif due and enabled:
                        issue(db, track, 'revoke')
                    elif now() >= (track.get('nextCheck') or 0):
                        issue(db, track, 'observe')
                    if due and not enabled:
                        track['phase'] = 'paused_due'
                save(db, track)
            export(target)
            atomic_json(dossier(target) / '心跳.json', {'pid': pid, 'processCreated': created, 'heartbeat': now()})
            if track['done']:
                break
            time.sleep(2)
    finally:
        event(target, 'worker_stopped', {'pid': pid})
        lock.close()


def supervise_key(target, track, owned):
    if track['done']:
        return
    pid, created = track.get('pid'), track.get('processCreated')
    child = owned.get(target)
    if child and child.poll() is None:
        if now() - getattr(child, '_guard_spawned_at', now()) < STALE_HEARTBEAT:
            return
    alive = created and identity(pid) == created
    if alive and now() - (track.get('heartbeat') or 0) <= STALE_HEARTBEAT:
        return
    if alive:
        event(target, 'stale_heartbeat', {'pid': pid, 'processCreated': created})
        # Invalidate browser jobs BEFORE stopping an unresponsive owner.
        with transaction() as db:
            latest = gettrack(db, target)
            if latest and latest.get('pid') == pid and latest.get('processCreated') == created:
                latest.update(epoch=latest['epoch'] + 1, needsReconcile=True, nextCheck=now(),
                              lastError='本单心跳失效；已作废旧任务并准备独立重启')
                close_jobs(db, target)
                save(db, latest)
        terminate_exact(pid, created)
        return
    directory = dossier(target)
    directory.mkdir(parents=True, exist_ok=True)
    ensure_lease(track)
    with (directory / '进程输出.log').open('ab', buffering=0) as output:
        child = subprocess.Popen([sys.executable, '-B', '-u', str(HERE / 'process_runtime.py'), 'worker', target],
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                 cwd=str(HERE), creationflags=subprocess.CREATE_NO_WINDOW)
    child._guard_spawned_at = now()
    owned[target] = child
    event(target, 'supervisor_spawned_worker', {'pid': child.pid})


def supervise():
    lock = Mutex('supervisor')
    if not lock.owned:
        lock.close()
        return
    owned = {}
    last_exports = 0
    try:
        while True:
            try:
                with transaction() as db:
                    at = now()
                    setmeta(db, 'supervisor', {'pid': os.getpid(), 'processCreated': identity(os.getpid()), 'heartbeat': at})
                    expire(db, at)
                    current = tracks(db)
            except Exception as error:
                print('监督状态读取暂时失败: ' + str(error), file=sys.stderr, flush=True)
                time.sleep(2)
                continue
            for target, track in current.items():
                try:
                    supervise_key(target, track, owned)
                except Exception as error:
                    # One lease conflict or failed launch must not stop other IDs.
                    try:
                        event(target, 'supervisor_key_error', str(error))
                        with transaction() as db:
                            latest = gettrack(db, target)
                            if latest:
                                latest['lastError'] = '独立进程启动/恢复受阻: ' + str(error)
                                save(db, latest)
                    except Exception as logging_error:
                        print(target + ' 状态写入受阻: ' + str(logging_error), file=sys.stderr, flush=True)
            if now() - last_exports >= 10000:
                for target in current:
                    try:
                        export(target)
                    except Exception as error:
                        print(target + ' JSON存档受阻: ' + str(error), file=sys.stderr, flush=True)
                last_exports = now()
            try:
                atomic_json(STORE / '监督心跳.json', {'pid': os.getpid(), 'processCreated': identity(os.getpid()), 'heartbeat': now()})
            except Exception as error:
                print('监督心跳JSON写入受阻: ' + str(error), file=sys.stderr, flush=True)
            time.sleep(2)
    finally:
        # Workers retain their own lifetime mutexes and do not depend on this parent.
        lock.close()


def main():
    if len(sys.argv) == 2 and sys.argv[1] == 'supervise':
        supervise()
    elif len(sys.argv) == 3 and sys.argv[1] == 'worker':
        worker(sys.argv[2])
    else:
        raise SystemExit('用法: python process_runtime.py supervise | worker 精确UUID')


if __name__ == '__main__':
    main()
