import { useRef, useState } from 'react';
import { ApiError, errorText, newId, post } from './api';
import RichText from './RichText';
import type { Evidence, StaffUpdateType } from './types';
import styles from './Workspace.module.css';

export default function StaffForm({
  taskId,
  inputVersion,
  evidence = [],
  refresh,
  conflict,
  onAuth,
}: {
  taskId: string;
  inputVersion: number;
  evidence?: Evidence[];
  refresh: () => Promise<void>;
  conflict: () => Promise<void>;
  onAuth: () => void;
}) {
  const [type, setType] = useState<StaffUpdateType>('progress');
  const [text, setText] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const pending = useRef<{ key: string; id: string } | null>(null);
  const submit = async () => {
    const body = {
      type,
      text: text.trim(),
      evidence_refs: evidence.filter((e) => selected.includes(`${e.kind}:${e.id}:${e.version}`)),
      expected_version: inputVersion,
    };
    const key = JSON.stringify(body);
    if (pending.current?.key !== key) pending.current = { key, id: newId() };
    setBusy(true);
    setError('');
    try {
      await post(`/staff/tasks/${encodeURIComponent(taskId)}/updates`, {
        ...body,
        client_event_id: pending.current.id,
      });
      setText('');
      setPreview(false);
      pending.current = null;
      setNotice('处理记录已由服务器保存。');
      await refresh();
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 409) {
        setPreview(false);
        await conflict();
      }
      if (e instanceof ApiError && e.status === 401) onAuth();
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className={styles.staffForm}>
      <h2>接续处理</h2>
      <p className={styles.meta}>操作者与时间由服务器记录。建议发布后仍等待消费者实际反馈。</p>
      <label>
        记录类型
        <select
          value={type}
          disabled={busy}
          onChange={(e) => {
            setType(e.target.value as StaffUpdateType);
            setPreview(false);
          }}
        >
          <option value="progress">处理进展</option>
          <option value="request_info">请求补充资料</option>
          <option value="advice">提供后续建议</option>
          <option value="resolved">提交已核验恢复结果</option>
          <option value="internal_note">内部备注（仅人工可见）</option>
        </select>
      </label>
      <label>
        处理内容
        <textarea
          rows={4}
          value={text}
          disabled={busy}
          maxLength={8000}
          onChange={(e) => {
            setText(e.target.value);
            setPreview(false);
          }}
        />
      </label>
      {!!evidence.length && (
        <fieldset>
          <legend>本次处理依据</legend>
          {evidence.map((e) => {
            const key = `${e.kind}:${e.id}:${e.version}`;
            return (
              <label className={styles.check} key={key}>
                <input
                  type="checkbox"
                  checked={selected.includes(key)}
                  disabled={busy}
                  onChange={(event) => {
                    setSelected((current) =>
                      event.target.checked ? [...current, key] : current.filter((k) => k !== key),
                    );
                    setPreview(false);
                  }}
                />
                {e.title || e.id} · 版本 {e.version}
              </label>
            );
          })}
        </fieldset>
      )}
      {type === 'resolved' && (
        <p className={styles.warning}>需要选择可核验的恢复依据。仅提供建议不能标记已恢复。</p>
      )}
      {preview && (
        <div className={styles.preview}>
          <strong>{type === 'internal_note' ? '内部备注预览 · 消费者不可见' : '消费者将看到以下内容'}</strong>
          <RichText>{text}</RichText>
          <button disabled={busy} onClick={() => void submit()}>
            {busy ? '保存中…' : type === 'internal_note' ? '确认保存内部备注' : '确认发布'}
          </button>
        </div>
      )}
      {!preview && (
        <button
          disabled={busy || !text.trim() || (type === 'resolved' && !selected.length)}
          onClick={() => setPreview(true)}
        >
          预览并核对
        </button>
      )}
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      <p role="status">{notice}</p>
    </section>
  );
}
