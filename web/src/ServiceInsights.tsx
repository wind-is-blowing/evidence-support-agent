import type { TaskSnapshot } from './types';
import { materialUrl } from './api';
import { Icon } from './Icon';
import styles from './Workspace.module.css';

export default function ServiceInsights({ task }: { task: TaskSnapshot }) {
  const signals = task.service_signals;
  const warranty = task.warranty;
  const images = task.image_observations || [];
  const candidates = task.purchase_lookup?.dealer_candidates || [];
  return (
    <div className={styles.insights}>
      {signals && signals.priority !== 'normal' && (
        <section className={styles.serviceFocus} aria-label="当前服务重点">
          <Icon name={signals.safety_risk ? 'shield' : 'clock'} size={20} />
          <div>
            <strong>{signals.safety_risk ? '安全优先 · 交由专员处理' : '已记下你的急用需求'}</strong>
            <p>
              {signals.safety_risk
                ? '常规排障已暂停，现有现象会随工单一起交接。'
                : task.urgency || signals.basis}
            </p>
          </div>
        </section>
      )}
      {images.length > 0 && (
        <section className={styles.insightCard} aria-label="图片观察结果">
          <h3>
            <Icon name="image" size={19} />
            图片里的线索
          </h3>
          {images.map((item) => (
            <div key={item.material_id} className={styles.imageObservation}>
              <a href={materialUrl(item.material_id)} target="_blank" rel="noreferrer">
                <img src={materialUrl(item.material_id)} alt="识别所依据的图片" />
              </a>
              <div>
                <strong>
                  {item.status !== 'ok'
                    ? '尚未识别图片'
                    : item.readable
                      ? '识别到的文字与部件'
                      : '画面不够清晰'}
                </strong>
                <p>{[...(item.visible_text || []), ...(item.components || [])].slice(0, 8).join(' · ')}</p>
                {item.uncertainties?.length ? (
                  <p className={styles.meta}>{item.uncertainties.join('；')}</p>
                ) : null}
              </div>
            </div>
          ))}
          <small className={styles.meta}>图片识别可能有误，请核对型号与错误码；凭证还需单独核验。</small>
        </section>
      )}
      {warranty && (
        <section className={styles.insightCard} aria-label="购买与质保核验">
          <h3>
            <Icon name="shield" size={19} />
            购买与质保核验
          </h3>
          <span className={styles.warrantyBadge} data-eligible={warranty.eligibility}>
            {warranty.eligibility === 'applicable'
              ? '模拟核验 · 在保修期限内'
              : warranty.eligibility === 'inapplicable'
                ? '模拟核验 · 已超出当前期限'
                : '待补充材料 · 尚不能判定'}
          </span>
          <p>{warranty.reason || '请登录后查询本人订单；查不到订单不等于没有质保。'}</p>
          {warranty.purchased_at && (
            <p>
              购买日期：{warranty.purchased_at}　截止日期：{warranty.expires_at}
            </p>
          )}
          {candidates.map((dealer) => (
            <p key={dealer.id}>
              <strong>{dealer.name}</strong>
              <br />
              {dealer.authorized && dealer.scope_matches
                ? '模拟名录匹配：渠道在授权范围内，仍需核验购买凭证。'
                : '名录信息或适用范围不足，请人工核验。'}
            </p>
          ))}
          <ul>
            {(task.purchase_lookup?.next_steps?.length
              ? task.purchase_lookup.next_steps
              : warranty.next_steps || []
            ).map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ul>
          {warranty.notice && <small className={styles.meta}>{warranty.notice}</small>}
        </section>
      )}
    </div>
  );
}
