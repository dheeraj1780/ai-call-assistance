import { ApiError } from "./api";

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "rate_limited") return "Too many attempts. Please wait a minute and try again.";
    if (error.code === "validation_error") return "Please check the highlighted fields.";
    return error.message;
  }
  return "Could not reach the server. Check your connection and try again.";
}

export const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const MIN_PASSWORD_LENGTH = 10;
