export type JobFeedbackCategory =
  | "closed"
  | "application_channel_unavailable"
  | "content_changed"
  | "incorrect_information";

export const FEEDBACK_CATEGORY_LABELS: Record<JobFeedbackCategory, string> = {
  closed: "职位已关闭",
  application_channel_unavailable: "投递渠道不可用",
  content_changed: "职位内容已变更",
  incorrect_information: "职位信息有误",
};

export interface JobFeedback {
  id: string;
  job_id: string;
  category: JobFeedbackCategory;
  note: string | null;
  created_at: string;
}

export interface AdminJobFeedback {
  id: string;
  job_id: string;
  category: JobFeedbackCategory;
  note: string | null;
  created_at: string;
}

export interface FeedbackList {
  total: number;
  feedbacks: JobFeedback[];
}

export interface AdminFeedbackList {
  total: number;
  feedbacks: AdminJobFeedback[];
}

export interface FeedbackCreatePayload {
  job_id: string;
  category: JobFeedbackCategory;
  note?: string;
}
