import { useState } from 'react';
import { evidenceUrl, materialUrl } from './api';
import { Icon } from './Icon';
import RichText from './RichText';
import type { Card, Device, EventType, Evidence, Receipt } from './types';
import styles from './Workspace.module.css';

export function EvidenceList({ items = [] }: { items?: Evidence[] }) {
  if (!items.length) return null;
  return (
    <details className={styles.evidence}>
      <summary>
        <Icon name="book" size={16} />
        查看资料依据 · {items.length}
        <span className={styles.evidenceArrow}>↗</span>
      </summary>
      {items.map((e, i) => (
        <section key={`${e.kind}-${e.id}-${i}`}>
          <strong>{e.title || `${e.kind} · ${e.id}`}</strong>
          <p className={styles.meta}>
            {[e.model, e.region, `版本 ${e.version}`, e.location].filter(Boolean).join(' · ')}
          </p>
          {e.excerpt && <RichText>{e.excerpt}</RichText>}
          {e.material_id ? (
            <a href={materialUrl(e.material_id)} target="_blank" rel="noreferrer">
              查看授权原件 ↗
            </a>
          ) : evidenceUrl(e.url) ? (
            <a href={evidenceUrl(e.url)} target="_blank" rel="noreferrer">
              查看来源 ↗
            </a>
          ) : null}
        </section>
      ))}
    </details>
  );
}

export function ActionCard({
  card,
  disabled,
  send,
  escalationAllowed = true,
  devices = [],
}: {
  card: Card;
  disabled: boolean;
  escalationAllowed?: boolean;
  devices?: Device[];
  send: (type: EventType, payload: Record<string, unknown>) => Promise<void>;
}) {
  const [reviewed, setReviewed] = useState(false);
  const [selectedDevices, setSelectedDevices] = useState<Record<string, string>>({});
  const base = { card_id: card.id, card_version: card.version };
  return (
    <section className={styles.actionCard} aria-label="当前操作" data-card={card.type}>
      <div className={styles.cardTop}>
        <span className={styles.cardIcon}>
          <Icon
            name={card.type === 'step' ? 'tool' : card.type === 'product' ? 'device' : 'service'}
            size={21}
          />
        </span>
        <div>
          <div className={styles.eyebrow}>下一步 · 等待你的确认</div>
          <span>
            {card.type === 'step'
              ? '按实际情况反馈，我们一起继续'
              : card.type === 'product'
                ? '准确的产品信息，是服务的起点'
                : '核对记录后，让服务继续向前'}
          </span>
        </div>
      </div>
      {card.type === 'product' && (
        <>
          <h2>先确认一下你的产品</h2>
          <p>{card.prompt || '请选择与你的设备一致的型号。'}</p>
          <div className={styles.candidates}>
            {card.candidates.map((product) => (
              <div className={styles.candidate} key={product.id}>
                {product.material_id && <img src={materialUrl(product.material_id)} alt={product.name} />}
                {!product.material_id && (
                  <span className={styles.candidateGlyph}>
                    <Icon name="device" size={28} />
                  </span>
                )}
                <strong>{product.name}</strong>
                <span>
                  {[product.category_label || product.category, product.model].filter(Boolean).join(' · ')}
                </span>
                <small>{product.description}</small>
                {devices.some((device) => device.product_id === product.id) && (
                  <label className={styles.deviceSelect}>
                    这一次是哪台设备？
                    <select
                      aria-label={`${product.name}的设备记录`}
                      disabled={disabled}
                      value={selectedDevices[product.id] || ''}
                      onChange={(event) =>
                        setSelectedDevices((current) => ({ ...current, [product.id]: event.target.value }))
                      }
                    >
                      <option value="">新设备，不关联历史设备</option>
                      {devices
                        .filter((device) => device.product_id === product.id)
                        .map((device) => (
                          <option key={device.id} value={device.id}>
                            {device.label} · {device.id.slice(0, 8)}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
                <button
                  type="button"
                  className="secondary"
                  disabled={disabled}
                  aria-label={`这是我的产品：${product.name} ${product.model}`}
                  onClick={() =>
                    void send('product_confirmed', {
                      ...base,
                      product_id: product.id,
                      ...(selectedDevices[product.id] ? { device_id: selectedDevices[product.id] } : {}),
                    })
                  }
                >
                  这是我的产品 →
                </button>
              </div>
            ))}
          </div>
          <button
            className="secondary"
            disabled={disabled}
            onClick={() => void send('product_rejected', base)}
          >
            都不是，补充线索
          </button>
        </>
      )}
      {card.type === 'step' && (
        <>
          <h2>{card.title}</h2>
          {card.product_name && <p className={styles.meta}>适用产品：{card.product_name}</p>}
          {!!card.prerequisites?.length && (
            <div>
              <strong>开始前请确认</strong>
              <ul>
                {card.prerequisites.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          {!!card.warnings?.length && (
            <div className={styles.warning}>
              <strong>
                <Icon name="alert" size={16} />
                安全提醒
              </strong>
              <ul>
                {card.warnings.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          <div className={styles.instruction}>
            <span>操作指引</span>
            <RichText>{card.instruction}</RichText>
          </div>
          {card.expected_result && (
            <p>
              <strong>预期现象：</strong>
              {card.expected_result}
            </p>
          )}
          <EvidenceList items={card.evidence_refs} />
          <p className={styles.meta}>操作后，告诉我们实际结果</p>
          <div className={styles.feedbackActions}>
            {[
              ['performed', '已操作'],
              ['no_change', '无变化'],
              ['unable', '无法执行'],
              ['recovered', '已恢复'],
            ].map(([feedback, label]) => (
              <button
                key={feedback}
                className={`secondary ${styles.feedbackButton}`}
                data-feedback={feedback}
                disabled={disabled}
                onClick={() =>
                  void send('step_feedback', {
                    step_instance_id: card.instance_id,
                    product_version: card.product_version,
                    feedback,
                  })
                }
              >
                <Icon
                  name={
                    feedback === 'performed'
                      ? 'check'
                      : feedback === 'no_change'
                        ? 'minus'
                        : feedback === 'unable'
                          ? 'pause'
                          : 'spark'
                  }
                  size={18}
                />
                {label}
              </button>
            ))}
          </div>
        </>
      )}
      {card.type === 'escalation' && (
        <>
          <h2>请确认交给专员的内容</h2>
          <p className={styles.simulation}>模拟业务环境 · 不会提交至真实品牌客服</p>
          <dl className={styles.facts}>
            {card.product && (
              <>
                <dt>已确认产品</dt>
                <dd>
                  {card.product.name} · {card.product.model}
                </dd>
              </>
            )}
            <dt>接收方</dt>
            <dd>{card.recipient}</dd>
            <dt>待解决问题</dt>
            <dd>{card.unresolved_issue || card.reason}</dd>
            {card.urgency && (
              <>
                <dt>明确时限（用户原话）</dt>
                <dd>{card.urgency}</dd>
              </>
            )}
          </dl>
          <RichText>{card.reason}</RichText>
          <h3>已经尝试</h3>
          {card.attempted_steps?.length ? (
            <ul>
              {card.attempted_steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          ) : (
            <p>尚无操作记录</p>
          )}
          {!!card.pending_checks?.length && (
            <>
              <h3>仍需核验</h3>
              <ul>
                {card.pending_checks.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            </>
          )}
          {!!card.material_ids?.length && (
            <>
              <h3>随附材料</h3>
              <div className={styles.thumbnails}>
                {card.material_ids.map((id, i) => (
                  <a key={id} href={materialUrl(id)} target="_blank" rel="noreferrer">
                    <img src={materialUrl(id)} alt={`材料 ${i + 1} 原图`} />
                  </a>
                ))}
              </div>
            </>
          )}
          <EvidenceList items={card.evidence_refs} />
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={reviewed}
              onChange={(e) => setReviewed(e.target.checked)}
              disabled={disabled}
            />
            我已核对完整内容，同意将上述材料交给此接收方
          </label>
          {escalationAllowed ? (
            <button
              disabled={disabled || !reviewed}
              onClick={() =>
                void send('escalation_confirmed', { draft_id: card.draft_id, draft_version: card.version })
              }
            >
              {disabled ? '等待服务器确认…' : '确认并提交模拟工单'}
            </button>
          ) : (
            <a href={`/login?next=${encodeURIComponent(location.pathname)}`}>
              登录账号并接续此任务后，确认提交 →
            </a>
          )}
        </>
      )}
    </section>
  );
}

const receiptLabels: Record<string, string> = {
  pending: '正在提交',
  submitting: '正在提交',
  unknown: '核验中',
  reconciling: '核验中',
  accepted: '未解决、已升级',
  submitted: '未解决、已升级',
  needs_info: '等待补充资料',
  resolved: '已核验处理结果',
  failed: '提交未完成',
};
export function ReceiptCard({ receipt }: { receipt: Receipt }) {
  return (
    <section className={styles.receipt}>
      <span className={styles.receiptIcon}>
        <Icon name="service" size={22} />
      </span>
      <div className={styles.eyebrow}>工单回执 · {receipt.is_simulated ? '模拟业务' : '业务记录'}</div>
      <h3>{receiptLabels[receipt.status.toLowerCase()] || receipt.status}</h3>
      <p>{receipt.message}</p>
      {receipt.ticket_id && <p className={styles.meta}>工单号：{receipt.ticket_id}</p>}
      {receipt.operation_id && <p className={styles.meta}>操作号：{receipt.operation_id}</p>}
    </section>
  );
}
