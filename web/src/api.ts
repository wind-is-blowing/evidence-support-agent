import type { Card, Evidence, Memory, Session, TaskSnapshot } from './types';

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public retryable = false,
    public request_id?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
let csrfToken = '';
export function setSessionToken(session: Session | null) {
  csrfToken = session?.csrf_token ?? '';
}
export const newId = () => crypto.randomUUID();

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const mutation = options.method && options.method !== 'GET';
  if (mutation && csrfToken) headers.set('X-CSRF-Token', csrfToken);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { ...options, headers, credentials: 'same-origin' });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'network_error', '连接暂时中断。内容已保留，请重试。', true);
  }
  if (!response.ok) {
    const raw = await response.json().catch(() => ({}));
    const data = raw.error || raw.detail || raw;
    throw new ApiError(
      response.status,
      data.code || 'request_failed',
      typeof data.message === 'string'
        ? data.message
        : response.status === 401
          ? '会话已失效，请重新登录。'
          : '请求未完成，请稍后重试。',
      Boolean(data.retryable),
      data.request_id,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body) });
export function errorText(error: unknown) {
  return error instanceof Error ? error.message : '请求未完成，请重试。';
}
export const materialUrl = (id: string) => `/api/materials/${encodeURIComponent(id)}`;
export function evidenceUrl(url?: string) {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
const str = (value: unknown, fallback = '') => (typeof value === 'string' ? value : fallback);
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const strings = (value: unknown) => list(value).filter((item): item is string => typeof item === 'string');
export function normalizeEvidence(value: unknown): Evidence[] {
  return list(value).map((item) => {
    if (typeof item === 'string') return { kind: 'reference', id: item, version: '未提供' };
    const e = record(item);
    return {
      ...e,
      kind: str(e.kind, 'document'),
      id: str(e.id || e.document_id),
      version: typeof e.version === 'number' || typeof e.version === 'string' ? e.version : '未提供',
      url: str(e.url || e.source_url),
      excerpt: str(e.excerpt || e.text),
      model: str(e.model) || strings(e.product_ids).join(' / '),
    } as Evidence;
  });
}
function stepTitle(raw: Record<string, unknown>): string {
  const title = str(raw.title).trim();
  return title && title !== str(raw.step_id) ? title : '当前排查步骤';
}

function normalizeCard(value: unknown): Card | null {
  const raw = record(value);
  if (!['product', 'step', 'escalation'].includes(str(raw.type))) return null;
  const base = {
    ...raw,
    id: str(raw.id || raw.step_instance_id || raw.draft_id),
    version: Number(raw.version || raw.draft_version || raw.source_version || raw.product_version || 0),
    evidence_refs: normalizeEvidence(raw.evidence_refs),
  };
  if (raw.type === 'step')
    return {
      ...base,
      type: 'step',
      instance_id: str(raw.instance_id || raw.step_instance_id),
      step_id: str(raw.step_id),
      title: stepTitle(raw),
      instruction: Array.isArray(raw.instructions)
        ? strings(raw.instructions).join('\n')
        : str(raw.instruction || raw.instructions),
      prerequisites: strings(raw.prerequisites),
      warnings: strings(raw.warnings),
      expected_result: str(raw.expected_result || raw.expected_feedback),
    } as Card;
  if (raw.type === 'escalation')
    return {
      ...base,
      type: 'escalation',
      draft_id: str(raw.draft_id),
      reason: str(raw.reason),
      recipient: str(raw.recipient),
      urgency: str(raw.urgency),
      attempted_steps: list(raw.attempted_steps).map((item) =>
        typeof item === 'string'
          ? item
          : [
              str(record(item).title),
              str(record(item).feedback || record(item).user_feedback),
              str(record(item).feedback_text),
            ]
              .filter(Boolean)
              .join(' · '),
      ),
      material_ids: raw.material_ids
        ? strings(raw.material_ids)
        : list(raw.materials)
            .map((item) => (typeof item === 'string' ? item : str(record(item).id)))
            .filter(Boolean),
      pending_checks: strings(raw.pending_checks || raw.missing_fields),
    } as Card;
  return { ...base, type: 'product', candidates: list(raw.candidates) } as Card;
}

/** Adapt the persisted API representation without deriving business outcomes in the browser. */
export function normalizeSnapshot(value: unknown): TaskSnapshot {
  const raw = record(value);
  const evidence = normalizeEvidence(raw.evidence);
  const resolveRefs = (value: unknown) =>
    normalizeEvidence(value).map(
      (ref) =>
        evidence.find(
          (item) => item.id === ref.id && (ref.kind === 'reference' || item.version === ref.version),
        ) || ref,
    );
  const card = normalizeCard(raw.current_card);
  if (card) card.evidence_refs = resolveRefs(card.evidence_refs);
  return {
    ...raw,
    waiting_reason: raw.waiting_reason || raw.wait_reason,
    current_card: card,
    messages: list(raw.messages).map((item) => {
      const m = record(item);
      return { ...m, evidence_refs: resolveRefs(m.evidence_refs) };
    }),
    steps: list(raw.steps || raw.step_history).map((item) => {
      const s = record(item);
      return {
        ...s,
        title: stepTitle(s),
        instance_id: s.instance_id || s.step_instance_id,
        user_feedback: s.user_feedback || s.feedback,
        evidence_refs: resolveRefs(s.evidence_refs),
      };
    }),
    receipts: list(raw.receipts),
    evidence_refs: evidence.length ? evidence : normalizeEvidence(raw.evidence_refs),
  } as TaskSnapshot;
}
export const getTask = async (id: string, options?: RequestInit) =>
  normalizeSnapshot(await api<unknown>(`/tasks/${encodeURIComponent(id)}`, options));
export function normalizeMemory(value: unknown): Memory {
  const m = record(value);
  return {
    ...m,
    text: str(m.text || m.content),
    created_at: str(m.created_at || m.occurred_at),
    evidence_refs: normalizeEvidence(m.evidence_refs || m.source_event_ids),
  } as Memory;
}
