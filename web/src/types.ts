export interface Session {
  principal: { id: string; role: 'guest' | 'consumer' | 'staff'; display_name: string };
  csrf_token: string;
  has_guest_session?: boolean;
}
export interface Evidence {
  kind: string;
  id: string;
  version: number | string;
  location?: string;
  title?: string;
  model?: string;
  region?: string;
  excerpt?: string;
  material_id?: string;
  url?: string;
}
export interface Material {
  id: string;
  url?: string;
  mime_type?: string;
  size_bytes?: number;
  name?: string;
}
export interface Message {
  id: string;
  role: string;
  text: string;
  created_at: string;
  material_ids?: string[];
  materials?: Material[];
  evidence_refs?: Evidence[];
  actor_name?: string;
  update_type?: string;
}
export interface Product {
  id: string;
  name: string;
  model: string;
  category?: string;
  category_label?: string;
  retail_sku?: string;
  description?: string;
  material_id?: string;
}
export interface Device {
  id: string;
  product_id: string;
  label: string;
  created_at?: string;
}
export interface Purchase {
  id: string;
  product_id: string;
  product_name: string;
  model: string;
  order_number: string;
  purchased_at?: string;
  status: string;
  channel?: string;
  region?: string;
  amount?: { currency?: string; value?: number };
  is_simulated?: boolean;
}
export interface BaseCard {
  id: string;
  version: number;
  product_version?: number;
  evidence_refs?: Evidence[];
}
export interface ProductCard extends BaseCard {
  type: 'product';
  candidates: Product[];
  prompt?: string;
}
export interface StepCard extends BaseCard {
  type: 'step';
  instance_id: string;
  step_id: string;
  title: string;
  instruction: string;
  product_name?: string;
  prerequisites?: string[];
  warnings?: string[];
  expected_result?: string;
}
export interface EscalationCard extends BaseCard {
  type: 'escalation';
  product?: Product;
  draft_id: string;
  recipient: string;
  reason: string;
  urgency?: string;
  unresolved_issue?: string;
  attempted_steps?: string[];
  material_ids?: string[];
  pending_checks?: string[];
  is_simulated?: boolean;
}
export type Card = ProductCard | StepCard | EscalationCard;
export interface Receipt {
  id: string;
  operation_id?: string;
  ticket_id?: string;
  status: string;
  message?: string;
  created_at?: string;
  is_simulated?: boolean;
}
export interface StepRecord {
  instance_id: string;
  title: string;
  product_name?: string;
  user_feedback?: string;
  evidence_refs?: Evidence[];
}
export interface TaskSummary {
  id: string;
  title: string;
  state: string;
  updated_at?: string;
  waiting_reason?: string;
}
export interface TaskSnapshot extends TaskSummary {
  revision: number;
  input_version: number;
  product_version: number;
  last_event_seq: number;
  messages: Message[];
  product?: Product | null;
  current_card?: Card | null;
  steps: StepRecord[];
  receipts: Receipt[];
  is_simulated: boolean;
  can_resume: boolean;
  model_mode?: string;
  pause_reason?: string;
  unresolved_issue?: string;
  urgency?: string;
  waiting_since?: string;
  service_signals?: {
    emotion: string;
    priority: string;
    safety_risk: boolean;
    basis: string;
    notice: string;
  };
  service_intent?: string;
  image_observations?: {
    material_id: string;
    status: string;
    readable?: boolean;
    visible_text?: string[];
    components?: string[];
    uncertainties?: string[];
  }[];
  purchase_lookup?: {
    dealer_candidates?: {
      id: string;
      name: string;
      authorized: boolean;
      scope_matches: boolean;
      is_simulated: boolean;
    }[];
    next_steps?: string[];
    conclusion?: string;
  };
  warranty?: {
    eligibility?: string;
    reason?: string;
    purchased_at?: string;
    expires_at?: string;
    policy_id?: string;
    notice?: string;
    missing_fields?: string[];
    next_steps?: string[];
    is_simulated?: boolean;
  };
  purchase?: Purchase & { text?: string; product_version?: number };
  evidence_refs?: Evidence[];
}
export interface ServerEvent {
  seq: number;
  type: string;
  payload: unknown;
  created_at?: string;
}
export interface Memory {
  id: string;
  text: string;
  version: number;
  created_at: string;
  device_id?: string;
  product_name?: string;
  evidence_refs: Evidence[];
}
export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}
export type EventType = components['schemas']['EventInput']['type'];
export type StaffUpdateType = components['schemas']['StaffUpdate']['type'];
export type MessageInput = components['schemas']['MessageInput'];
export type EventInput = components['schemas']['EventInput'];
import type { components } from './generated/api';
