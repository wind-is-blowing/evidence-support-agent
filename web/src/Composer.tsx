import { useEffect, useRef, useState } from 'react';
import { api, ApiError, errorText, materialUrl, newId, post } from './api';
import type { Material } from './types';
import { Icon } from './Icon';
import styles from './Workspace.module.css';

interface Upload {
  key: string;
  file: File;
  preview: string;
  material?: Material;
  error?: string;
  uploading: boolean;
}
export default function Composer({
  taskId,
  refresh,
  onAuth,
}: {
  taskId: string;
  refresh: () => Promise<void>;
  onAuth: () => void;
}) {
  const [text, setText] = useState('');
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const pending = useRef<{ fingerprint: string; client_event_id: string } | null>(null);
  const previews = useRef(new Set<string>());
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      for (const url of previews.current) URL.revokeObjectURL(url);
    };
  }, []);
  const upload = async (item: Upload) => {
    setUploads((items) =>
      items.map((u) => (u.key === item.key ? { ...u, uploading: true, error: undefined } : u)),
    );
    const body = new FormData();
    body.append('file', item.file);
    try {
      const material = await api<Material>(`/tasks/${encodeURIComponent(taskId)}/uploads`, {
        method: 'POST',
        body,
      });
      if (active.current)
        setUploads((items) =>
          items.map((u) => (u.key === item.key ? { ...u, material, uploading: false } : u)),
        );
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onAuth();
      if (active.current)
        setUploads((items) =>
          items.map((u) => (u.key === item.key ? { ...u, error: errorText(e), uploading: false } : u)),
        );
    }
  };
  const selectFiles = (files: FileList | null) => {
    if (!files) return;
    setError('');
    if (uploads.length + files.length > 3) {
      setError('每条消息最多附带 3 张图片。');
      return;
    }
    for (const file of files) {
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 10 * 1024 * 1024) {
        setError('请选择不超过 10 MiB 的 JPEG、PNG 或 WebP 图片。');
        return;
      }
    }
    const items = [...files].map((file) => {
      const preview = URL.createObjectURL(file);
      previews.current.add(preview);
      return { key: newId(), file, preview, uploading: true };
    });
    setUploads((current) => [...current, ...items]);
    for (const item of items) void upload(item);
  };
  const send = async () => {
    if (sending || uploads.some((u) => !u.material) || (!text.trim() && !uploads.length)) return;
    const body = {
      text: text.trim(),
      material_ids: uploads.map((u) => u.material!.id),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    };
    const fingerprint = JSON.stringify(body);
    if (pending.current?.fingerprint !== fingerprint)
      pending.current = { fingerprint, client_event_id: newId() };
    setSending(true);
    setError('');
    setNotice('');
    try {
      await post(`/tasks/${encodeURIComponent(taskId)}/messages`, {
        ...body,
        client_event_id: pending.current.client_event_id,
      });
      pending.current = null;
      setText('');
      setUploads([]);
      setNotice('消息已发送，反馈会同步到服务记录。');
      await refresh();
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 401) onAuth();
    } finally {
      setSending(false);
    }
  };
  return (
    <form
      className={styles.composer}
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      <div className={styles.quickPrompts} aria-label="常用服务请求">
        <button
          type="button"
          disabled={sending}
          onClick={() => setText('想核对这台产品的保修资格，请帮我查一下购买记录。')}
        >
          查保修
        </button>
        <button
          type="button"
          disabled={sending}
          onClick={() => setText('请整理现有问题、图片和操作记录，转人工接续。')}
        >
          转人工
        </button>
        <button type="button" disabled={sending} onClick={() => setText('我补充一下图片上的错误码：')}>
          补充错误码
        </button>
      </div>
      <label htmlFor="message-input">
        <Icon name="message" size={17} />
        继续描述你的问题
      </label>
      <textarea
        id="message-input"
        placeholder="描述现象、设备型号，或告诉我们上一步的结果…"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={2}
        maxLength={16000}
        disabled={sending}
      />
      {!!uploads.length && (
        <div className={styles.attachments}>
          {uploads.map((item) => (
            <div key={item.key}>
              <a
                href={item.material ? materialUrl(item.material.id) : item.preview}
                target="_blank"
                rel="noreferrer"
              >
                <img src={item.preview} alt={item.file.name} />
              </a>
              <small>{item.uploading ? '正在上传…' : item.error ? '上传失败' : '已上传，尚未核验'}</small>
              {item.error && (
                <button type="button" className="textButton" onClick={() => void upload(item)}>
                  重试上传
                </button>
              )}
              <button
                type="button"
                className="textButton"
                disabled={sending || item.uploading}
                onClick={() => setUploads((items) => items.filter((u) => u.key !== item.key))}
              >
                移除
              </button>
            </div>
          ))}
        </div>
      )}
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      <p role="status" className={styles.meta}>
        {notice}
      </p>
      <div className={styles.composerFooter}>
        <label className={styles.uploadButton}>
          <Icon name="image" size={18} />
          添加图片
          <input
            aria-label="添加图片"
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            disabled={sending || uploads.length >= 3}
            onChange={(e) => {
              selectFiles(e.target.files);
              e.target.value = '';
            }}
          />
        </label>
        <button
          type="submit"
          disabled={sending || uploads.some((u) => !u.material) || (!text.trim() && !uploads.length)}
        >
          {sending ? '发送中…' : error && pending.current ? '重试发送' : '发送消息 ↑'}
        </button>
      </div>
      <small className={styles.meta}>
        仅 JPEG / PNG / WebP，最多 3 张、每张 10 MiB。上传完成不代表凭证已核验。
      </small>
    </form>
  );
}
