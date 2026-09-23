export type Role = "OWNER" | "ADMIN" | "MEMBER";

export interface User {
  id: string;
  email: string;
  full_name: string;
  phone: string | null;
}

export interface CompanySummary {
  id: string;
  name: string;
}

export interface Company extends CompanySummary {
  industry: string | null;
  description: string | null;
  website: string | null;
  products_services: string | null;
  target_customer: string | null;
  ai_instructions: string | null;
  transcript_retention_days: number;
  created_at: string;
  updated_at: string;
}

export interface SessionInfo {
  user: User;
  company: CompanySummary;
  role: Role;
}

export interface AuthResponse extends SessionInfo {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
}

export interface ErrorBody {
  code: string;
  message: string;
  details: unknown;
  request_id: string | null;
}
