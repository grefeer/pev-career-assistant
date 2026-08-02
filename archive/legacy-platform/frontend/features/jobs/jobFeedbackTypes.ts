export type JobFeedbackCategory =
  | "closed"
  | "application_channel_unavailable"
  | "content_changed"
  | "incorrect_information";
export type JobFeedbackStatus = "open" | "accepted" | "resolved" | "rejected" | "withdrawn";
export type FeedbackStudentAction = "upsert" | "withdraw";
export type FeedbackAdminDecision = "accept" | "resolve" | "reject";

export const FEEDBACK_CATEGORY_LABELS: Record<JobFeedbackCategory, string> = {
  closed: "职位已关闭",
  application_channel_unavailable: "投递渠道不可用",
  content_changed: "职位内容已变更",
  incorrect_information: "职位信息有误",
};

export interface StudentFeedbackItem {
  id: string; job_id: string; category: JobFeedbackCategory; status: JobFeedbackStatus;
  note: string | null; version: number; created_at: string; updated_at: string;
}
export interface StudentFeedbackListResponse { feedback: StudentFeedbackItem[] }
export interface FeedbackMutationRequest {
  action: FeedbackStudentAction; category: JobFeedbackCategory;
  expected_version: number | null; note: string | null;
}
export interface FeedbackMutationResponse {
  id: string; job_id: string; category: JobFeedbackCategory; status: JobFeedbackStatus;
  version: number; updated_at: string;
}
export interface AdminFeedbackDetail extends StudentFeedbackItem {
  company_name: string; title: string; job_status: string; job_review_version: number;
}
export interface AdminFeedbackAggregate {
  job_id: string; company_name: string; title: string; category: JobFeedbackCategory;
  open_count: number; accepted_count: number; total_count: number; latest_updated_at: string;
}
export interface AdminFeedbackQueueResponse {
  total: number; feedback: AdminFeedbackDetail[]; aggregates: AdminFeedbackAggregate[];
}
export interface AdminFeedbackDecisionRequest {
  decision: FeedbackAdminDecision; expected_version: number;
}
