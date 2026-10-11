"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import { fetchTeam, updateTeamMember } from "@/lib/api/workspace";
import type { TeamMember, UserRole } from "@/lib/types";
import { useCurrentUser } from "@/lib/use-current-user";

const ROLE_LABEL: Record<UserRole, string> = {
  admin: "Admin",
  recruiter: "Recruiter",
  hiring_manager: "Hiring manager",
  interviewer: "Interviewer",
};

function DepartmentField({ member, disabled, onSave }: { member: TeamMember; disabled: boolean; onSave: (value: string | null) => void }) {
  const [value, setValue] = useState(member.department ?? "");
  return (
    <input
      value={value}
      onChange={(event) => setValue(event.target.value)}
      onBlur={() => value.trim() !== (member.department ?? "") && onSave(value.trim() || null)}
      onKeyDown={(event) => event.key === "Enter" && event.currentTarget.blur()}
      maxLength={100}
      disabled={disabled}
      placeholder="—"
      aria-label={`Department for ${member.full_name}`}
      className="w-full min-w-28 rounded-md border border-transparent bg-transparent px-2 py-1 text-sm hover:border-border focus:border-border disabled:hover:border-transparent"
    />
  );
}

export default function TeamPage() {
  const me = useCurrentUser();
  const [team, setTeam] = useState<TeamMember[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const isAdmin = me?.user.role === "admin";

  useEffect(() => {
    fetchTeam()
      .then(setTeam)
      .catch((caught: unknown) => setError(caught instanceof ApiError ? caught.message : "Could not load the team."));
  }, []);

  const save = async (member: TeamMember, body: { department?: string | null; is_active?: boolean }) => {
    setBusyId(member.id);
    setError(null);
    try {
      const updated = await updateTeamMember(member.id, body);
      setTeam((current) => current?.map((item) => (item.id === updated.id ? updated : item)) ?? null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The change could not be saved.");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Team</h1>
        <p className="text-sm text-muted">
          Roles and sign-in are managed in your identity provider (Clerk) and sync on each member&apos;s next sign-in.
          {isAdmin ? " Admins can set departments and deactivate access here." : ""}
        </p>
      </div>
      {error && (
        <div className="space-y-1">
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
          {!team && (
            <Link href="/workspace/session" className="text-sm font-medium text-brand hover:underline">
              Set up a workspace session
            </Link>
          )}
        </div>
      )}
      {!team ? (
        !error && <p className="text-sm text-muted">Loading…</p>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-border bg-surface">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-muted">
              <tr>
                <th scope="col" className="px-4 py-3 font-medium">Name</th>
                <th scope="col" className="px-4 py-3 font-medium">Role</th>
                <th scope="col" className="px-4 py-3 font-medium">Department</th>
                <th scope="col" className="px-4 py-3 font-medium">Joined</th>
                <th scope="col" className="px-4 py-3 font-medium">Access</th>
              </tr>
            </thead>
            <tbody>
              {team.map((member) => {
                const self = member.id === me?.user.id;
                return (
                  <tr key={member.id} className={`border-t border-border ${member.is_active ? "" : "text-muted"}`}>
                    <td className="px-4 py-2.5">
                      <p className="font-medium">
                        {member.full_name}
                        {self && <span className="ml-1.5 text-xs font-normal text-muted">(you)</span>}
                      </p>
                      <p className="text-xs text-muted">{member.email}</p>
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs" title="Managed in Clerk">
                        {ROLE_LABEL[member.role]}
                      </span>
                    </td>
                    <td className="px-2 py-2.5">
                      {isAdmin ? (
                        <DepartmentField
                          member={member}
                          disabled={busyId === member.id}
                          onSave={(department) => void save(member, { department })}
                        />
                      ) : (
                        <span className="px-2">{member.department ?? "—"}</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-muted">
                      {new Date(member.created_at).toLocaleDateString(undefined, { dateStyle: "medium" })}
                    </td>
                    <td className="px-4 py-2.5">
                      {isAdmin && !self ? (
                        <button
                          type="button"
                          onClick={() => void save(member, { is_active: !member.is_active })}
                          disabled={busyId === member.id}
                          className={`rounded-lg border border-border px-2.5 py-1 text-xs font-medium disabled:opacity-60 ${
                            member.is_active ? "text-danger" : "text-foreground"
                          }`}
                        >
                          {member.is_active ? "Deactivate" : "Reactivate"}
                        </button>
                      ) : (
                        <span className={`text-xs ${member.is_active ? "text-success" : "text-muted"}`}>
                          {member.is_active ? "● Active" : "Deactivated"}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
