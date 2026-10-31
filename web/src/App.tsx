import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, errorText, materialUrl, newId, normalizeMemory, post, setSessionToken } from './api';
import { ActionCard, EvidenceList, ReceiptCard } from './Cards';
import Composer from './Composer';
import { Icon, ServiceArt } from './Icon';
import RichText from './RichText';
import StaffForm from './StaffForm';
import ServiceInsights from './ServiceInsights';
import { useTask } from './useTask';
import type { Device, EventType, Memory, Page, Purchase, Session, TaskSnapshot, TaskSummary } from './types';
import styles from './Workspace.module.css';

const labels: Record<string, string> = {
  READY: '等待处理',
  RUNNING: '正在处理',
  WAITING: '等待下一步',
  PAUSED: '已暂停',
  COMPLETED: '已确认恢复',
  CANCELLED: '已停止',
};
const feedbackLabels: Record<string, string> = {
  performed: '已操作',
  no_change: '无变化',
  unable: '无法执行',
  recovered: '已恢复',
};
const stateLabel = (state: string) => labels[state] || state;
const reasonLabels: Record<string, string> = {
  user_input: '等待补充问题描述',
  product_confirmation: '等待确认具体产品',
  purchase_selected: '订单与商品已匹配，请描述要咨询的问题',
  step_feedback: '等待你的实际操作反馈',
  escalation_confirmation: '等待核对升级材料',
  submitting: '工单正在提交，请等待实际回执',
  action_unknown: '工单回执仍在核验中，暂不重复提交',
  human_service: '问题尚未解决，等待人工接续',
  budget_exhausted: '处理额度已用尽，请联系维护者调整额度',
  decision_budget_exhausted: '本轮处理达到次数上限，已暂停。',
  tool_budget_exhausted: '本轮查询和核验达到次数上限，已暂停。',
  request_budget_exhausted: '本轮服务请求达到次数上限，已暂停。',
  task_or_daily_budget_exhausted: '本次服务或今日的处理额度已用尽，请稍后重试或联系服务维护人员。',
  context_budget_exceeded: '本轮处理上下文已达到模型输入上限，已有进度已保存。',
  knowledge_review_required: '已找到相关资料，等待补充信息或人工接续。',
  turn_deadline: '本轮处理超时，已有服务记录已保存。',
  knowledge_changed: '相关产品资料已更新，继续前需要重新核对。',
  stale_decision: '服务记录已有更新，请根据最新信息继续。',
  stale_task: '服务记录已有更新，请根据最新信息继续。',
  worker_error: '本轮处理遇到问题，已有服务记录已保存。',
  retry_deferred: '服务暂时繁忙，已有服务记录已保存，请稍后继续。',
  operation_unknown: '上次操作的结果仍待核验，请稍后查看服务记录。',
  action_reconciliation: '工单提交结果正在核验，请等待实际回执。',
  user_stopped: '已按你的要求停止后续处理',
  model_timeout: '本轮处理超时，已保留之前的服务记录',
};
function localTime(value?: string) {
  return value && Number.isFinite(Date.parse(value))
    ? new Intl.DateTimeFormat('zh-CN', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }).format(new Date(value))
    : '';
}
function safeNext() {
  const next = new URLSearchParams(location.search).get('next');
  return next && /^\/(tasks|staff\/tasks)(\/[^/?#]+)?$/.test(next) ? next : '/tasks';
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const updateSession = (value: Session) => {
    setSessionToken(value);
    setSession(value);
  };
  useEffect(() => {
    void api<Session>('/session')
      .then(updateSession)
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) setError(errorText(e));
      })
      .finally(() => setLoading(false));
  }, []);
  const expired = useCallback(() => {
    setSessionToken(null);
    setSession(null);
    setError('会话已失效，请重新登录后继续。');
  }, []);
  const logout = async () => {
    try {
      await post('/session/logout', {});
      setSessionToken(null);
      location.assign('/login');
    } catch (e) {
      setError(errorText(e));
    }
  };
  const path = location.pathname;
  const staff = session?.principal.role === 'staff';
  if (loading)
    return (
      <main className={styles.loading} role="status">
        正在读取服务记录…
      </main>
    );
  if (!session || path === '/login')
    return <Login session={session} error={error} onSession={updateSession} />;
  return (
    <>
      <header className={styles.header}>
        <a className={styles.brand} href={staff ? '/staff/tasks' : '/tasks'}>
          <span className={styles.brandMark}>
            <Icon name="service" size={25} />
          </span>
          <span>
            安心服务<small>每一步，都有依据</small>
          </span>
        </a>
        <div className={styles.headerContext}>
          <span>服务中心</span>
          <span>/</span>
          <strong>{staff ? '人工接续工作台' : '我的产品服务'}</strong>
        </div>
        <div className={styles.account}>
          <span className={styles.accountName}>
            <span className={styles.avatar}>
              <Icon name="user" size={16} />
            </span>
            {session.principal.display_name || (staff ? '人工客服' : '访客')}
          </span>
          {!staff && session.principal.role !== 'guest' && (
            <a href="/me/memories">
              <Icon name="book" size={16} />
              记忆管理
            </a>
          )}
          {session.principal.role === 'guest' ? (
            <a href={`/login?next=${encodeURIComponent(path)}`}>登录账号</a>
          ) : (
            <button className="textButton" onClick={() => void logout()}>
              退出
            </button>
          )}
        </div>
      </header>
      {error && (
        <p className={styles.globalError} role="alert">
          {error}
        </p>
      )}
      {path === '/me/memories' ? (
        <Memories onAuth={expired} />
      ) : (
        <Workspace session={session} staff={staff} onAuth={expired} />
      )}
    </>
  );
}

function Login({
  session,
  error: outerError,
  onSession,
}: {
  session: Session | null;
  error: string;
  onSession: (session: Session) => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [displayName, setDisplayName] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [signedIn, setSignedIn] = useState<Session | null>(null);
  const [adoptionPending, setAdoptionPending] = useState(false);
  const [adopt, setAdopt] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(outerError);
  const guestTask = safeNext().match(/^\/tasks\/([^/]+)$/)?.[1];
  const ensureGuest = async () => {
    if (session) return session;
    const guest = await post<Session>('/session/guest', {});
    onSession(guest);
    return guest;
  };
  const login = async () => {
    if (mode === 'register') {
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$/.test(username)) {
        setError('账号需为 3–64 位英文字母、数字、下划线、横线或点，首位为字母或数字。');
        return;
      }
      if (!displayName.trim() || displayName.trim().length > 80) {
        setError('请填写 1–80 个字符的显示名称。');
        return;
      }
      if (password.length < 6 || password.length > 128) {
        setError('密码需为 6–128 个字符，空格会原样保留。');
        return;
      }
      if (password !== confirmPassword) {
        setError('两次输入的密码不一致，请重新确认。');
        return;
      }
    }
    setBusy(true);
    setError('');
    try {
      await ensureGuest();
      const account = await post<Session>(mode === 'register' ? '/session/register' : '/session/login', {
        username,
        password,
        ...(mode === 'register' ? { display_name: displayName.trim() } : {}),
      });
      onSession(account);
      setSignedIn(account);
      if (adopt && guestTask && account.principal.role !== 'staff') {
        try {
          await post('/session/adopt', { task_ids: [guestTask] });
        } catch (e) {
          setAdoptionPending(true);
          throw e;
        }
      }
      location.assign(
        account.principal.role === 'staff' ? '/staff/tasks' : guestTask && !adopt ? '/tasks' : safeNext(),
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const retryAdoption = async () => {
    if (!signedIn || !guestTask) return;
    setBusy(true);
    setError('');
    try {
      await post('/session/adopt', { task_ids: [guestTask] });
      location.assign(safeNext());
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const guest = async () => {
    setBusy(true);
    setError('');
    try {
      await ensureGuest();
      location.assign(safeNext());
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  };
  return (
    <main className={styles.loginLayout}>
      <section className={styles.welcome}>
        <a className={styles.brand} href="/">
          <span className={styles.brandMark}>
            <Icon name="service" size={25} />
          </span>
          <span>安心服务</span>
        </a>
        <div className={styles.welcomeContent}>
          <div className={styles.eyebrow}>产品售后助手</div>
          <h1>
            把问题说清楚，
            <br />
            把下一步交给我们。
          </h1>
          <p>
            从确认产品，到排查故障，再到专员接续。
            <br />
            你的每次尝试，都有记录。
          </p>
          <div className={styles.welcomeArt}>
            <ServiceArt />
          </div>
          <div className={styles.welcomeSteps}>
            <span>
              <Icon name="device" />
              确认产品
            </span>
            <span>
              <Icon name="tool" />
              一步步处理
            </span>
            <span>
              <Icon name="message" />
              持续跟进
            </span>
          </div>
        </div>
        <small>本系统的工单与购买记录为模拟业务环境。</small>
      </section>
      <section className={styles.loginPanel}>
        <span className={styles.loginIcon}>
          <Icon name="lock" size={24} />
        </span>
        {adoptionPending ? (
          <>
            <div className={styles.eyebrow}>账号已就绪</div>
            <h2>继续接续你的服务记录</h2>
            <p className={styles.meta}>你已登录，当前访客任务尚未归入账号。可以重试，或先进入工作台。</p>
            {error && (
              <p role="alert" className={styles.error}>
                {error}
              </p>
            )}
            <button disabled={busy} onClick={() => void retryAdoption()}>
              {busy ? '正在归入记录…' : '重试归入记录'}
            </button>
            <button className="secondary" disabled={busy} onClick={() => location.assign('/tasks')}>
              进入工作台
            </button>
          </>
        ) : (
          <>
            <div className={styles.eyebrow}>{mode === 'login' ? '欢迎回来' : '从这里开始'}</div>
            <h2>{mode === 'login' ? '继续你的服务' : '创建你的服务账号'}</h2>
            <p className={styles.meta}>
              {mode === 'login'
                ? '登录后接续产品服务，也可以先以访客身份咨询。'
                : '保存产品与服务记录，下次从这里继续。'}
            </p>
            <div role="tablist" aria-label="账号入口" className={styles.authTabs}>
              {(['login', 'register'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  role="tab"
                  aria-selected={mode === value}
                  disabled={busy}
                  onClick={() => {
                    setMode(value);
                    setError('');
                    setConfirmPassword('');
                  }}
                >
                  {value === 'login' ? '登录' : '注册'}
                </button>
              ))}
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void login();
              }}
            >
              <label>
                账号
                <input
                  autoComplete="username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                  disabled={busy}
                  aria-describedby={mode === 'register' ? 'username-rule' : undefined}
                />
              </label>
              {mode === 'register' && (
                <>
                  <p id="username-rule" className={styles.fieldHint}>
                    3–64 位英文字母、数字、_、- 或 .，首位为字母或数字。
                  </p>
                  <label>
                    显示名称
                    <input
                      autoComplete="nickname"
                      value={displayName}
                      onChange={(e) => setDisplayName(e.target.value)}
                      required
                      maxLength={80}
                      disabled={busy}
                    />
                  </label>
                </>
              )}
              <label>
                密码
                <input
                  type="password"
                  autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  disabled={busy}
                  aria-describedby={mode === 'register' ? 'password-rule' : undefined}
                />
              </label>
              {mode === 'register' && (
                <>
                  <p id="password-rule" className={styles.fieldHint}>
                    6–128 个字符；密码中的空格会原样保留。
                  </p>
                  <label>
                    确认密码
                    <input
                      type="password"
                      autoComplete="new-password"
                      value={confirmPassword}
                      onChange={(e) => setConfirmPassword(e.target.value)}
                      required
                      disabled={busy}
                    />
                  </label>
                </>
              )}
              {guestTask && (
                <label className={styles.check}>
                  <input
                    type="checkbox"
                    checked={adopt}
                    onChange={(e) => setAdopt(e.target.checked)}
                    disabled={busy}
                  />
                  登录后接续当前访客任务
                </label>
              )}
              {error && (
                <p role="alert" className={styles.error}>
                  {error}
                </p>
              )}
              <button disabled={busy}>
                {busy ? '正在连接…' : mode === 'register' ? '注册并开始 →' : '登录并继续 →'}
              </button>
            </form>
            <button className="secondary" disabled={busy} onClick={() => void guest()}>
              以访客身份开始
            </button>
            <p className={styles.meta}>访客可查公开知识与本人材料。账号登录不代表购买资格已核验。</p>
          </>
        )}
      </section>
    </main>
  );
}

function Workspace({ session, staff, onAuth }: { session: Session; staff: boolean; onAuth: () => void }) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const createKey = useRef(newId());
  const match = location.pathname.match(/^\/(?:staff\/)?tasks\/([^/]+)$/);
  const id = match?.[1];
  const load = useCallback(
    async (cursor?: string) => {
      try {
        const data = await api<Page<TaskSummary>>(
          `${staff ? '/staff' : ''}/tasks${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`,
        );
        setTasks((current) =>
          cursor
            ? [...current, ...data.items.filter((item) => !current.some((old) => old.id === item.id))]
            : data.items,
        );
        setNextCursor(data.next_cursor);
      } catch (e) {
        setError(errorText(e));
        if (e instanceof ApiError && e.status === 401) onAuth();
      }
    },
    [staff, onAuth],
  );
  useEffect(() => {
    void load();
  }, [load]);
  const create = async (opening?: string) => {
    setBusy(true);
    setError('');
    try {
      const task = await post<TaskSnapshot>('/tasks', {
        create_key: createKey.current,
        title: '新的产品求助',
      });
      if (opening) {
        await post(`/tasks/${encodeURIComponent(task.id)}/messages`, {
          text: opening,
          material_ids: [],
          client_event_id: newId(),
          timezone: 'Asia/Shanghai',
        });
      }
      location.assign(`/tasks/${encodeURIComponent(task.id)}`);
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 401) onAuth();
      setBusy(false);
    }
  };
  const wrongRole = location.pathname.startsWith('/staff') && !staff;
  return (
    <div className={styles.workspace}>
      <aside className={styles.sidebar}>
        <div className={styles.sidebarTop}>
          <div className={styles.workspaceLabel}>你的服务空间</div>
          <a className={styles.sidebarHome} href={staff ? '/staff/tasks' : '/tasks'}>
            <Icon name="grid" size={19} />
            <span>{staff ? '人工工作台' : '服务工作台'}</span>
            <Icon name="arrow" size={15} />
          </a>
          {!staff && (
            <button className={styles.newTask} disabled={busy} onClick={() => void create()}>
              ＋ 新的产品求助
            </button>
          )}
          <details className={styles.taskList} open={window.matchMedia('(min-width: 761px)').matches}>
            <summary>
              {staff ? '已分配给我的任务' : '我的服务记录'} <span>{tasks.length}</span>
            </summary>
            <nav aria-label="服务记录">
              {tasks.map((task) => (
                <a
                  key={task.id}
                  className={task.id === id ? styles.selectedTask : styles.taskLink}
                  aria-current={task.id === id ? 'page' : undefined}
                  href={`${staff ? '/staff' : ''}/tasks/${encodeURIComponent(task.id)}`}
                >
                  <div className={styles.taskLinkTitle}>
                    <Icon name="message" size={16} />
                    <strong>{task.title}</strong>
                  </div>
                  <span className={styles.taskLinkState} data-state={task.state}>
                    {stateLabel(task.state)}
                  </span>
                  <small>{localTime(task.updated_at)}</small>
                </a>
              ))}
            </nav>
            {!tasks.length && (
              <p className={styles.meta}>
                {staff ? '尚无分配任务，请联系维护者。' : '你的服务记录会保存在这里。'}
              </p>
            )}
            {nextCursor && (
              <button className="textButton" onClick={() => void load(nextCursor)}>
                加载更多
              </button>
            )}
          </details>
        </div>
        <div className={styles.sidebarFoot}>
          <span className={styles.sidebarShield}>
            <Icon name="shield" size={22} />
          </span>
          <strong>每一步，都有依据</strong>
          <p>
            服务记录随时接续，
            <br />
            你的信息由你掌握。
          </p>
        </div>
      </aside>
      {wrongRole ? (
        <main className={styles.empty}>
          <h1>无法访问此任务</h1>
          <p>请使用获分配此任务的人工账号登录。</p>
        </main>
      ) : id ? (
        <TaskView
          key={`${session.principal.id}-${id}`}
          id={id}
          staff={staff}
          authenticated={session.principal.role !== 'guest'}
          onAuth={onAuth}
        />
      ) : (
        <main className={styles.empty}>
          <div className={styles.emptyArt}>
            <ServiceArt />
          </div>
          <div className={styles.eyebrow}>{staff ? '人工接续工作台' : '从这里开始'}</div>
          <h1>{staff ? '让服务继续向前' : '今天，哪件产品需要帮助？'}</h1>
          <p>
            {staff
              ? '从左侧选择已分配的任务，查看原始对话、实际反馈和材料。'
              : '告诉我们设备型号和遇到的现象。我们会先确认产品，再一起找到下一步。'}
          </p>
          {!staff && (
            <button disabled={busy} onClick={() => void create()}>
              开始新的求助 →
            </button>
          )}
          <div className={styles.startGuide}>
            <section>
              <Icon name="device" />
              <strong>先确认产品</strong>
              <p>型号、图片或你熟悉的名称</p>
            </section>
            <section>
              <Icon name="tool" />
              <strong>一步步排查</strong>
              <p>每次只处理清楚的下一步</p>
            </section>
            <section>
              <Icon name="service" />
              <strong>需要时专员接续</strong>
              <p>带着真实记录继续跟进</p>
            </section>
          </div>
          {!staff && (
            <div className={styles.scenarios} aria-label="试试这些服务场景">
              <button
                disabled={busy}
                onClick={() => void create('我的 S1 Pro 不吸了，明天要开派对，急死了！')}
              >
                <span>01 / 急用排障</span>
                <strong>S1 Pro 不吸了</strong>
                <small>确认同名产品，接着排查</small>
              </button>
              <button
                disabled={busy}
                onClick={() => void create('我的 S1 Pro 是在安心数码买的，直营系统查不到订单，还能保修吗？')}
              >
                <span>02 / 渠道核验</span>
                <strong>查不到订单，也能继续</strong>
                <small>核对经销商与购买凭证</small>
              </button>
              <button
                disabled={busy}
                onClick={() => void create('我的 S1 Pro 已经试过排查，还是不行，请转人工处理。')}
              >
                <span>03 / 人工接续</span>
                <strong>带着已有记录交接</strong>
                <small>先核对材料，再确认升级</small>
              </button>
            </div>
          )}
        </main>
      )}
      {error && (
        <div role="alert" className={styles.globalError}>
          {error}
        </div>
      )}
    </div>
  );
}

function TaskView({
  id,
  staff,
  authenticated,
  onAuth,
}: {
  id: string;
  staff: boolean;
  authenticated: boolean;
  onAuth: () => void;
}) {
  const {
    snapshot: task,
    connection,
    error,
    notice,
    stale,
    refresh,
    conflict,
    setNotice,
  } = useTask(id, onAuth);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [devices, setDevices] = useState<Device[]>([]);
  const [purchases, setPurchases] = useState<Purchase[]>([]);
  const conversationRef = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  useEffect(() => {
    if (!authenticated || staff) return;
    const controller = new AbortController();
    void api<Page<Device>>('/me/devices', { signal: controller.signal })
      .then((data) => setDevices(data.items))
      .catch((error) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        if (error instanceof ApiError && error.status === 401) onAuth();
        else setNotice('已有设备记录暂未读取，产品确认将创建新设备记录。');
      });
    return () => controller.abort();
  }, [authenticated, staff, task?.product_version, onAuth, setNotice]);
  useEffect(() => {
    if (!task || !followLatest.current) return;
    const frame = requestAnimationFrame(() => {
      const conversation = conversationRef.current;
      if (conversation) conversation.scrollTop = conversation.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [task, task?.current_card?.id, task?.messages.length, task?.receipts.length]);
  useEffect(() => {
    if (!authenticated || staff) return;
    const controller = new AbortController();
    void api<Page<Purchase>>('/me/purchases', { signal: controller.signal })
      .then((data) => setPurchases(data.items))
      .catch((error) => {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        if (error instanceof ApiError && error.status === 401) onAuth();
        else setNotice('购物订单暂未读取，你仍可直接描述产品和问题。');
      });
    return () => controller.abort();
  }, [authenticated, staff, task?.product_version, onAuth, setNotice]);
  const pending = useRef(new Map<string, string>());
  const send = async (type: EventType, payload: Record<string, unknown>) => {
    if (!task || busy || stale) return;
    const body = { type, payload, expected_version: task.input_version };
    const key = JSON.stringify(body);
    if (!pending.current.has(key)) pending.current.set(key, newId());
    setBusy(true);
    setActionError('');
    try {
      await post(`/tasks/${encodeURIComponent(id)}/events`, {
        ...body,
        client_event_id: pending.current.get(key),
      });
      setNotice('反馈已保存，正在更新服务记录。');
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setActionError('信息已更新，请查看当前步骤');
        await conflict();
      } else setActionError(errorText(e));
      if (e instanceof ApiError && e.status === 401) onAuth();
    } finally {
      setBusy(false);
    }
  };
  if (!task)
    return (
      <main className={styles.empty}>
        {error ? (
          <>
            <h1>暂时无法读取任务</h1>
            <p role="alert">{error}</p>
            <button onClick={() => void refresh().catch((e) => setActionError(errorText(e)))}>
              重新读取
            </button>
            {actionError && <p role="alert">{actionError}</p>}
          </>
        ) : (
          <p role="status">正在读取服务记录…</p>
        )}
      </main>
    );
  const allEvidence = [
    ...(task.evidence_refs || []),
    ...(task.current_card?.evidence_refs || []),
    ...task.messages.flatMap((m) => m.evidence_refs || []),
  ].filter(
    (e, i, array) =>
      array.findIndex((x) => x.id === e.id && x.version === e.version && x.kind === e.kind) === i,
  );
  const pauseReason = task.pause_reason || task.waiting_reason || '';
  const pendingTicket =
    task.receipts.some((receipt) =>
      ['pending', 'submitting', 'unknown', 'reconciling'].includes(receipt.status.toLowerCase()),
    ) ||
    (task.current_card?.type === 'escalation' &&
      ['submitting', 'action_unknown', 'action_reconciliation'].includes(pauseReason));
  return (
    <>
      <main className={styles.taskMain}>
        <div
          ref={conversationRef}
          className={styles.conversationScroll}
          data-testid="conversation-scroll"
          role="region"
          aria-label="对话与处理记录"
          tabIndex={0}
          onScroll={(event) => {
            const conversation = event.currentTarget;
            followLatest.current =
              conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 120;
          }}
        >
          <div className={styles.taskHeader}>
            <div>
              <div className={styles.eyebrow}>
                <Icon name="message" size={14} />
                服务记录 {staff && '· 人工接续'}
              </div>
              <h1>{task.title}</h1>
            </div>
            <span className={styles.status} data-state={task.state}>
              {stateLabel(task.state)}
            </span>
          </div>
          {task.product && (
            <div className={styles.mobileProduct}>
              <Icon name="device" size={17} />
              <strong>{task.product.name}</strong>
              <span>{task.product.model}</span>
              <Icon name="check" size={16} />
              {!staff && (
                <button
                  className="textButton"
                  disabled={busy}
                  onClick={() => void send('product_correction', {})}
                >
                  更正产品
                </button>
              )}
            </div>
          )}
          <p className={styles.simulation}>
            <Icon name="shield" size={15} />
            {task.is_simulated ? '演示环境 · 工单不会发送至真实品牌客服' : '服务进度以已保存回执为准'}
          </p>
          <div role="status" aria-live="polite" className={styles.connection} data-connection={connection}>
            {connection === 'online'
              ? '服务记录已同步'
              : connection === 'loading'
                ? '正在连接…'
                : '连接中断，正在恢复最新记录…'}
            {notice && ` · ${notice}`}
          </div>
          {task.waiting_reason && reasonLabels[task.waiting_reason] && (
            <p className={styles.currentReason}>
              <Icon name="clock" size={15} />
              {reasonLabels[task.waiting_reason]}
            </p>
          )}
          {(error || actionError) && (
            <p role="alert" className={styles.error}>
              {actionError || error}
            </p>
          )}
          <section aria-label="对话记录" className={styles.timeline}>
            {!task.messages.length && (
              <div className={styles.startMessage}>
                <strong>你好，我会陪你一起处理。</strong>
                <p>请在下方描述产品和故障现象，也可以附上清晰的图片。</p>
              </div>
            )}
            {task.messages.map((message) => (
              <article
                key={message.id}
                className={message.role === 'user' ? styles.userMessage : styles.message}
              >
                <div className={styles.messageMeta}>
                  <span className={styles.messageAvatar}>
                    <Icon name={message.role === 'user' ? 'user' : 'service'} size={16} />
                  </span>
                  <strong>
                    {message.role === 'user'
                      ? '你'
                      : message.role === 'staff'
                        ? message.actor_name || '人工客服'
                        : '服务助手'}
                  </strong>
                  {message.update_type === 'internal_note' && (
                    <span className={styles.status}>内部备注 · 消费者不可见</span>
                  )}
                  <time dateTime={message.created_at}>{localTime(message.created_at)}</time>
                </div>
                {message.role === 'user' ? (
                  <p className={styles.plain}>{message.text}</p>
                ) : (
                  <RichText>{message.text}</RichText>
                )}
                <div className={styles.thumbnails}>
                  {(message.material_ids || message.materials?.map((m) => m.id) || []).map((material, i) => (
                    <a key={material} href={materialUrl(material)} target="_blank" rel="noreferrer">
                      <img src={materialUrl(material)} alt={`消息附图 ${i + 1}，点击查看原图`} />
                    </a>
                  ))}
                </div>
                <EvidenceList items={message.evidence_refs} />
              </article>
            ))}
          </section>
          <ServiceInsights task={task} />
          {task.current_card && (
            <ActionCard
              devices={devices}
              key={`${task.current_card.id}-${task.current_card.version}`}
              card={task.current_card}
              disabled={staff || busy || stale}
              escalationAllowed={authenticated}
              send={send}
            />
          )}
          {task.receipts.map((receipt) => (
            <ReceiptCard key={receipt.id} receipt={receipt} />
          ))}
          {task.state === 'PAUSED' && (
            <section className={styles.warning}>
              <h3>处理已暂停</h3>
              <p>{reasonLabels[pauseReason] || '处理暂时中断，已有服务记录已保存。'}</p>
              <p>
                {pendingTicket
                  ? '工单提交结果仍在核验中，请等待核验结果，避免重复提交。'
                  : task.can_resume
                    ? '你可以补充问题信息，或点击“继续处理”接着进行。'
                    : '已有服务记录会保留，你可以补充说明，等待具备继续处理的条件。'}
              </p>
            </section>
          )}
          {!staff && (
            <div className={styles.taskControls}>
              {task.can_resume && (
                <button disabled={busy || stale} onClick={() => void send('resume', {})}>
                  继续处理
                </button>
              )}
              {['RUNNING', 'READY', 'WAITING'].includes(task.state) && (
                <button className="textButton" disabled={busy || stale} onClick={() => void send('stop', {})}>
                  停止处理
                </button>
              )}
            </div>
          )}
        </div>
        {staff ? (
          <StaffForm
            taskId={id}
            inputVersion={task.input_version}
            evidence={allEvidence}
            refresh={refresh}
            conflict={conflict}
            onAuth={onAuth}
          />
        ) : (
          <Composer taskId={id} refresh={refresh} onAuth={onAuth} />
        )}
      </main>
      <aside className={styles.progressPane}>
        {!staff && authenticated && (
          <details className={styles.ordersPane}>
            <summary>
              <Icon name="bag" size={17} />
              我的购物订单
              <span>{purchases.length}</span>
            </summary>
            <section>
              {purchases.length ? (
                <div className={styles.orderList}>
                  {purchases.map((purchase) => {
                    const selected = task.purchase?.id === purchase.id;
                    return (
                      <article key={purchase.id} className={styles.orderCard} data-selected={selected}>
                        <div>
                          <strong>{purchase.product_name}</strong>
                          <span>{purchase.model}</span>
                        </div>
                        <p>{purchase.order_number}</p>
                        <small>
                          {localTime(purchase.purchased_at)} ·{' '}
                          {purchase.status === 'delivered' ? '已送达' : purchase.status}
                        </small>
                        <button
                          disabled={busy || stale || selected}
                          onClick={() => void send('purchase_selected', { purchase_id: purchase.id })}
                        >
                          {selected ? '当前咨询订单' : '就此订单咨询'}
                        </button>
                      </article>
                    );
                  })}
                </div>
              ) : (
                <p className={styles.meta}>当前账号暂无可用订单</p>
              )}
              <small className={styles.orderNotice}>赛事模拟订单仅用于演示商品匹配与售后核验。</small>
            </section>
          </details>
        )}
        <details open>
          <summary>
            <Icon name="grid" size={17} />
            产品与处理进度
          </summary>
          <section className={styles.productSummary}>
            <div className={styles.productGlyph}>
              <Icon name="device" size={34} />
            </div>
            <div className={styles.eyebrow}>当前产品</div>
            {task.product ? (
              <>
                <h2>{task.product.name}</h2>
                <p>{task.product.model}</p>
                <span className={styles.status} data-state="COMPLETED">
                  已确认
                </span>
                {!staff && (
                  <button
                    className="textButton"
                    disabled={busy}
                    onClick={() => void send('product_correction', {})}
                  >
                    更正产品
                  </button>
                )}
              </>
            ) : (
              <p>待确认 · 请先提供型号线索</p>
            )}
          </section>
          {task.urgency && (
            <section>
              <h3>用户的紧迫需求</h3>
              <p>{task.urgency}</p>
            </section>
          )}
          {task.waiting_since && <p className={styles.meta}>开始等待：{localTime(task.waiting_since)}</p>}
          <section>
            <h3>
              <Icon name="clock" size={16} />
              已经尝试
            </h3>
            {task.steps.length ? (
              <ol className={styles.history}>
                {task.steps.map((step) => (
                  <li key={step.instance_id}>
                    <strong>{step.title}</strong>
                    <span>
                      {feedbackLabels[step.user_feedback || ''] || step.user_feedback || '等待反馈'}
                    </span>
                    {step.product_name && <small>{step.product_name}</small>}
                  </li>
                ))}
              </ol>
            ) : (
              <p className={styles.meta}>尚无已执行步骤</p>
            )}
          </section>
          {task.purchase && (
            <section>
              <h3>购买与资格信息</h3>
              <p>{task.purchase.text || task.purchase.status || '待核验'}</p>
              {task.purchase.is_simulated && <small>模拟记录 · 不代表真实购买资格</small>}
            </section>
          )}
          {task.unresolved_issue && (
            <section>
              <h3>待解决问题</h3>
              <p>{task.unresolved_issue}</p>
            </section>
          )}
          <EvidenceList items={allEvidence} />
          <p className={styles.assurance}>
            <Icon name="shield" size={19} />
            仅依据你的实际反馈和核验结果确认恢复。
          </p>
        </details>
      </aside>
    </>
  );
}

function Memories({ onAuth }: { onAuth: () => void }) {
  const [items, setItems] = useState<Memory[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<Memory | null>(null);
  const [text, setText] = useState('');
  const pending = useRef(new Map<string, string>());
  const load = useCallback(async () => {
    try {
      const data = await api<Page<Memory>>('/me/memories');
      setItems(data.items.map(normalizeMemory));
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 401) onAuth();
    }
  }, [onAuth]);
  useEffect(() => {
    void load();
  }, [load]);
  const mutate = async (memory: Memory, action: 'corrections' | 'forget') => {
    const body = {
      expected_version: memory.version,
      ...(action === 'corrections' ? { text: text.trim() } : {}),
    };
    const key = `${memory.id}:${action}:${JSON.stringify(body)}`;
    if (!pending.current.has(key)) pending.current.set(key, newId());
    setBusy(memory.id);
    setError('');
    setNotice('');
    try {
      await post(`/me/memories/${encodeURIComponent(memory.id)}/${action}`, {
        ...body,
        client_event_id: pending.current.get(key),
      });
      await load();
      setEditing(null);
      setNotice(action === 'forget' ? '服务器已确认遗忘，并更新相关记录。' : '更正已保存，相关记录已刷新。');
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 409) await load();
      if (e instanceof ApiError && e.status === 401) onAuth();
    } finally {
      setBusy(null);
    }
  };
  return (
    <main className={styles.memories}>
      <a href="/tasks">← 返回服务记录</a>
      <div className={styles.eyebrow}>由你掌握的服务记忆</div>
      <h1>记得有用的，改正不准确的。</h1>
      <p className={styles.meta}>这里仅显示有效记忆。来源和设备范围帮助你核对每条记录。</p>
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      <p role="status">{notice}</p>
      {!items.length && <p className={styles.startMessage}>当前没有有效记忆。</p>}
      {items.map((memory) => (
        <section className={styles.memory} key={memory.id}>
          <RichText>{memory.text}</RichText>
          <p className={styles.meta}>
            {memory.product_name || memory.device_id || '账号范围'} · {localTime(memory.created_at)} · 版本{' '}
            {memory.version}
          </p>
          <EvidenceList items={memory.evidence_refs} />
          {editing?.id === memory.id ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void mutate(memory, 'corrections');
              }}
            >
              <label>
                更正内容
                <textarea
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  rows={3}
                  maxLength={2000}
                  required
                  disabled={!!busy}
                />
              </label>
              <div className={styles.actions}>
                <button disabled={!!busy || !text.trim()}>确认更正</button>
                <button
                  type="button"
                  className="secondary"
                  disabled={!!busy}
                  onClick={() => setEditing(null)}
                >
                  取消
                </button>
              </div>
            </form>
          ) : (
            <div className={styles.actions}>
              <button
                className="secondary"
                disabled={!!busy}
                onClick={() => {
                  setEditing(memory);
                  setText(memory.text);
                }}
              >
                更正
              </button>
              <button className="textButton" disabled={!!busy} onClick={() => void mutate(memory, 'forget')}>
                {busy === memory.id ? '等待服务器确认…' : '遗忘这条记忆'}
              </button>
            </div>
          )}
        </section>
      ))}
    </main>
  );
}
