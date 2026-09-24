import { useQuery } from "@tanstack/react-query";

import { crm, type Member } from "./crm";

export function useMembers() {
  return useQuery({
    queryKey: ["members"],
    queryFn: () => crm.listMembers(),
    staleTime: 5 * 60_000,
  });
}

export function memberName(members: Member[] | undefined, userId: string | null): string {
  if (!userId) return "Unassigned";
  return members?.find((m) => m.user_id === userId)?.full_name ?? "Former member";
}
