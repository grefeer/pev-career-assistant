export interface UserProfile {
  account: string;
  nickname: string;
  role: "student" | "admin";
  created_at: string;
  last_login_at: string;
}

export interface AuthResponse {
  ok: boolean;
  message: string;
  token?: string | null;
  profile?: UserProfile | null;
}
