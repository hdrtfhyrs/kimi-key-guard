"""Bounded Chrome native messages: registered leases and authentic screenshot files."""
import base64
import hashlib
import json
import math
import os
import re
import struct
import sys
sys.dont_write_bytecode = True
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
from guard_paths import DATA_ROOT
ROOT = DATA_ROOT
MAX_MESSAGE = 32 * 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat()


def data(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def write_once(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    write_once(temporary, data(value))
    os.replace(temporary, path)


def checked_id(value, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ValueError('本单精确API ID无效')
    return value


def same_time(left, right):
    return datetime.fromisoformat(str(left).replace('Z','+00:00')) == datetime.fromisoformat(str(right).replace('Z','+00:00'))


def lease_path(target):
    return ROOT / 'leases' / checked_id(target) / '租期.json'


def registered(target):
    destination = lease_path(target)
    if destination.exists():
        return json.loads(destination.read_text(encoding='utf-8'))
    raise ValueError('本单尚未登记实际发出时间与额度起点；手动撤销请在弹窗明确确认')


def handle(message):
    if not isinstance(message, dict):
        raise ValueError('消息须为对象')
    action = message.get('action')
    if isinstance(action, str) and action.startswith('process_'):
        import process_runtime
        return process_runtime.handle(message)
    if action == 'status':
        receipt = {'ok':True, 'root':str(ROOT), 'connectedAt':stamp(),
                   'callerOrigin':sys.argv[1] if len(sys.argv)>1 else None,
                   'extensionVersion':message.get('extensionVersion'),
                   'source':'Chrome native messaging actual call'}
        atomic_json(ROOT / '运行入口回执.json', receipt)
        return receipt
    target = checked_id(message.get('targetId'), optional=action=='save_current')
    if action == 'register_lease':
        lease = message.get('lease')
        if not isinstance(lease,dict) or lease.get('id')!=target or lease.get('name')!='5小时':
            raise ValueError('只登记本人确认的5小时服务单')
        sent = datetime.fromisoformat(str(lease.get('sentAt')).replace('Z','+00:00'))
        if sent.tzinfo is None:
            raise ValueError('发出时间须带时区')
        baseline = float(lease.get('baseline'))
        if not math.isfinite(baseline) or not 0 <= baseline <= 100:
            raise ValueError('用量起点无效')
        destination = lease_path(target)
        if destination.exists():
            previous=json.loads(destination.read_text(encoding='utf-8'))
            if not same_time(previous.get('sentAt'),lease.get('sentAt')) or float(previous.get('baseline'))!=baseline:
                raise ValueError('本单已登记且起点不同，保留原记录，不覆盖')
        else:
            write_once(destination,data({**lease,'registeredAt':stamp(),'callerOrigin':sys.argv[1] if len(sys.argv)>1 else None}))
        return {'ok':True,'targetId':target,'leasePath':str(destination)}
    if action == 'save_state':
        registered(target)
        state=message.get('state')
        if not isinstance(state,dict) or state.get('id')!=target:
            raise ValueError('本单状态ID不符')
        directory=lease_path(target).parent
        record={'storedAt':stamp(),'state':state}
        atomic_json(directory / '当前状态.json',record)
        with (directory / '用量记录.jsonl').open('ab') as output:
            output.write((json.dumps(record,ensure_ascii=False)+'\n').encode('utf-8'))
            output.flush()
            os.fsync(output.fileno())
        return {'ok':True,'statePath':str(directory / '当前状态.json')}
    if action in {'save_before','save_current'}:
        before = action=='save_before'
        manual_context = None
        if before and message.get('manualRequestId'):
            import process_runtime
            manual_context = process_runtime.handle({'action':'process_manual_evidence','id':target,
                'jobId':message.get('jobId'),'epoch':message.get('epoch'),'manualRequestId':message['manualRequestId']})
            if not manual_context.get('ok'):
                raise ValueError(manual_context.get('error','手动确认未通过'))
        lease = registered(target) if before and not manual_context else None
        uri=message.get('screenshot','')
        if not isinstance(uri,str) or not uri.startswith('data:image/png;base64,'):
            raise ValueError('必须提供Chrome捕获的真实PNG')
        pixels=base64.b64decode(uri.split(',',1)[1],validate=True)
        if len(pixels)<33 or not pixels.startswith(b'\x89PNG\r\n\x1a\n') or pixels[12:16]!=b'IHDR':
            raise ValueError('PNG头无效，禁止删除')
        width,height=struct.unpack('>II',pixels[16:24])
        if width<400 or height<300:
            raise ValueError('截图过小，无法看清页面')
        page=message.get('page',{})
        if not isinstance(page,dict) or not str(page.get('url','')).startswith('https://www.kimi.com/code/console'):
            raise ValueError('证据页面不是Kimi控制台')
        text=page.get('text','')
        if not isinstance(text,str) or (before and target not in text):
            raise ValueError('删除前真实页面没有本单精确ID')
        snapshot=message.get('snapshot',{})
        if not isinstance(snapshot,dict):
            raise ValueError('用量记录无效')
        if manual_context and (snapshot.get('id')!=target or process_runtime.millis(snapshot.get('sentAt'))!=manual_context['track'].get('sentAt')):
            raise ValueError('手动截图与本ID原记录不符')
        if before and not manual_context and not same_time(snapshot.get('sentAt'),lease.get('sentAt')):
            raise ValueError('截图租期与登记原件不符，禁止删除')
        evidence_id=uuid.uuid4().hex
        directory=ROOT / 'screenshots' / (target or '页面') / evidence_id
        directory.mkdir(parents=True,exist_ok=False)
        image_path=directory / ('删除前真实页面.png' if before else '当前真实页面.png')
        write_once(image_path,pixels)
        digest=hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest!=hashlib.sha256(pixels).hexdigest():
            raise ValueError('截图落盘校验失败，禁止删除')
        purpose='before_deletion' if before else message.get('purpose','current_page_only')
        record={'targetId':target,'evidenceId':evidence_id,'capturedAt':message.get('capturedAt'),'storedAt':stamp(),
                'reason':message.get('reason'),'snapshot':snapshot,'page':{k:page.get(k) for k in ['url','title','refreshedAt','visibleUsage','targetPresent']},
                'screenshot':str(image_path),'sha256':digest,'dimensions':{'width':width,'height':height},
                'purpose':purpose,'deletionPerformedAtSave':False,
                'manualRequestId':message.get('manualRequestId') if manual_context else None,
                'jobId':message.get('jobId') if manual_context else None,
                'trigger':'本人确认手动撤销' if manual_context else '自动停止条件' if before else '保存当前页面'}
        write_once(directory / '刷新后的页面正文.txt',text.encode('utf-8'))
        record_path=directory / ('删除前记录.json' if before else '当前页面记录.json')
        write_once(record_path,data(record))
        lines=['删除前真实证据' if before else '真实页面存档（本动作没有执行删除）','',
               f"截图时间：{record['capturedAt']}",f"存档时间：{record['storedAt']}",f'精确API ID：{target}',
               f"用途：{purpose}",f"原因：{record['reason']}",f"实际发出：{snapshot.get('sentAt')}",
               f"到期：{snapshot.get('deadline')}",f"发出时起点：{snapshot.get('baseline')}%",
               f"本单结转：{snapshot.get('carry')}%",f"最近接口窗口读数：{snapshot.get('windowPercent')}%",
               f"最近接口用量时间：{snapshot.get('usageObservedAt')}",f"本次取证是否另取最新接口用量：{snapshot.get('usageSnapshotFresh')}",
               f"本单最近累计计算值：{snapshot.get('consumed')}%",f"刷新后网页频限显示：{page.get('visibleUsage')}",
               f'原始PNG：{image_path.name}',f'SHA-256：{digest}',
               '截图为刷新后的原始网页，累计计算值单独记录，不修改网页数字。',
               '本次由本人确认立即撤销触发，不以到期或累计100%为由，不改变实际交付字段。' if manual_context else '到期撤销以本人登记的发出时间加五小时判断；未核历史累计时保留空值，不将其写成0%。',
               '已结束或未登记单的当前页面存档不能证明该买家后续耗用，也不能冒称先前的删除前图。']
        write_once(directory / '证据说明.txt',('\n'.join(lines)+'\n').encode('utf-8'))
        return {'ok':True,'targetId':target,'evidenceId':evidence_id,'screenshotPath':str(image_path),'sha256':digest,'directory':str(directory)}
    if action=='save_result':
        evidence_id=message.get('evidenceId','')
        if not isinstance(evidence_id,str) or not re.fullmatch(r'[a-f0-9]{32}',evidence_id):
            raise ValueError('证据ID无效')
        directory=ROOT / 'screenshots' / target / evidence_id
        before=json.loads((directory / '删除前记录.json').read_text(encoding='utf-8'))
        if not before.get('manualRequestId'):
            registered(target)
        if before.get('targetId')!=target or hashlib.sha256((directory / '删除前真实页面.png').read_bytes()).hexdigest()!=before['sha256']:
            raise ValueError('删除前证据校验失败')
        result={'targetId':target,'evidenceId':evidence_id,'storedAt':stamp(),'deletedAt':message.get('deletedAt'),
                'deletedAtSource':'删除接口成功响应时刻；服务端实际时刻未知',
                'confirmedAbsentAt':message.get('confirmedAbsentAt'),'revokeIntentAt':message.get('revokeIntentAt'),
                'reason':message.get('reason'),'confirmedAbsent':bool(message.get('confirmedAbsent')),
                'deleteResponseConfirmed':bool(message.get('deleteResponseConfirmed')),'beforeScreenshotSha256':before['sha256']}
        destination=directory / '删除结果.json'
        if not destination.exists():
            write_once(destination,data(result))
        state=message.get('state')
        if isinstance(state,dict) and state.get('id')==target:
            atomic_json(lease_path(target).parent / '当前状态.json',{'storedAt':stamp(),'state':state})
        import process_runtime
        completed=process_runtime.handle({'action':'process_record_result','id':target,'evidenceId':evidence_id})
        if not completed.get('ok'):
            raise ValueError('图片和删除结果已存D，界面状态待重核：'+str(completed.get('error')))
        return {'ok':True,'resultPath':str(destination)}
    raise ValueError('不支持的存档动作')


def main():
    if os.name=='nt':
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(),os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(),os.O_BINARY)
    manifest=json.loads((HERE / 'native-host.json').read_text(encoding='utf-8'))
    origin=sys.argv[1] if len(sys.argv)>1 else ''
    if origin.rstrip('/') not in {item.rstrip('/') for item in manifest['allowed_origins']}:
        return
    # connectNative persists until Chrome closes the port. Legacy one-frame
    # callers disconnect after their reply, with identical payload semantics.
    while True:
        header=sys.stdin.buffer.read(4)
        if len(header)!=4:
            return
        length=struct.unpack('<I',header)[0]
        message=None
        malformed=False
        try:
            if length<=0 or length>MAX_MESSAGE:
                malformed=True
                raise ValueError('消息长度越界')
            chunks=[]
            remaining=length
            while remaining:
                part=sys.stdin.buffer.read(remaining)
                if not part:
                    malformed=True
                    raise ValueError('消息不完整')
                chunks.append(part)
                remaining-=len(part)
            message=json.loads(b''.join(chunks).decode('utf-8'))
            response=handle(message)
        except Exception as error:
            response={'ok':False,'error':str(error)}
        if isinstance(message,dict) and 'requestId' in message:
            response['requestId']=message['requestId']
        encoded=json.dumps(response,ensure_ascii=False).encode('utf-8')
        sys.stdout.buffer.write(struct.pack('<I',len(encoded)))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        if malformed:
            return


if __name__=='__main__':
    main()
