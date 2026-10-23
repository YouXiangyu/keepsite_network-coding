import { useEffect, useRef, useState } from 'react';
import { ArrowClockwise } from '@phosphor-icons/react';
import { ApiError, assertSessionEpoch, errorMessage, getOperationStatus, mutate, sessionEpoch, type Capabilities } from '../../api';
import type { Ticket } from '../../data';
import type { RunMutation, RunReconciliation } from '../../app/useAccountWorkspace';
import { processingActive, processingDescription, processingLabels } from './processing';

export default function ProcessingStatus({ ticket, capabilities, busy, runMutation, runReconciliation }: {
  ticket: Ticket; capabilities?: Capabilities; busy: boolean; runMutation: RunMutation; runReconciliation: RunReconciliation;
}) {
  const operation = useRef<string | null>(null);
  const alive = useRef(true);
  const [notice, setNotice] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const enabled = !!capabilities?.processing && ticket.media.original.cloudStatus === 'saved';
  const active = processingActive(ticket.processing.status);
  const onError = (error: unknown) => {
    if (!alive.current) return;
    setNotice(errorMessage(error));
    setFailed(true);
    setUncertain(!(error instanceof ApiError) || error.status === 0 || error.status === 409 || error.status >= 500);
  };
  const onCancel = () => {
    if (!alive.current) return;
    setUncertain(true); setFailed(true); setNotice('处理请求已中断，结果尚未确认，请核对后继续。');
  };
  const start = () => {
    if (!enabled || busy || active && !uncertain) return;
    operation.current ??= crypto.randomUUID();
    const operationId = operation.current;
    void runMutation(() => mutate(`/api/tickets/${encodeURIComponent(ticket.id)}/process`, 'POST', { operationId }), {
      onStart: () => setNotice(''), onError, onCancel,
      onSuccess: () => {
        if (!alive.current) return;
        operation.current = null; setUncertain(false); setFailed(false); setNotice('处理请求已确认，进度会自动更新。');
      },
    });
  };
  const reconcile = () => {
    if (!operation.current || busy) return;
    const operationId = operation.current;
    let applied = false;
    void runReconciliation(async () => {
      const scope = sessionEpoch();
      const receipt = await getOperationStatus(operationId);
      assertSessionEpoch(scope);
      applied = receipt.applied;
      return receipt;
    }, {
      onStart: () => setNotice(''), onError, onCancel,
      onSuccess: () => {
        if (!alive.current) return;
        if (applied) operation.current = null;
        setUncertain(!applied); setFailed(false);
        setNotice(applied ? '处理请求已核对，当前进度已更新。' : '上次处理请求尚未确认，可以使用同一请求重试。');
      },
    });
  };
  const currentStep = ['queued', 'ocr', 'poster'].indexOf(ticket.processing.status);
  return <section className="processing-panel" aria-label="自动处理">
    <h2>自动处理</h2>
    <p className={`processing-state ${active ? 'is-processing' : ''}`} role="status">{processingLabels[ticket.processing.status]}</p>
    {active && <ol className="processing-steps" aria-label="处理阶段">{['排队', '识别票面', '检索海报'].map((label, index) => <li key={label} aria-current={index === currentStep ? 'step' : undefined} data-complete={index < currentStep}>{label}</li>)}</ol>}
    <p>{processingDescription(ticket.processing.status)}</p>
    {ticket.processing.error && <p className="processing-error" role={ticket.processing.status === 'failed' ? 'alert' : 'status'}>{ticket.processing.error}</p>}
    {!enabled && <p className="processing-unavailable">{!capabilities?.processing ? capabilities?.processingUnavailableReason || '处理配置尚未就绪，当前可手动填写。' : '原票尚未保存到云端，暂不能开始自动处理。'}</p>}
    {notice && <p className={failed ? 'form-error' : 'processing-notice'} role={failed ? 'alert' : 'status'}>{notice}</p>}
    <div className="action-row">
      <button className="secondary-button" disabled={!enabled || busy || active && !uncertain} onClick={start}><ArrowClockwise size={18} />{uncertain ? '重试同一处理请求' : ticket.processing.status === 'not_started' ? '开始识别' : active ? '处理中…' : ticket.processing.status === 'ready' ? '重新处理' : '重试处理'}</button>
      {uncertain && <button className="secondary-button" disabled={busy} onClick={reconcile}>核对处理请求</button>}
    </div>
  </section>;
}
