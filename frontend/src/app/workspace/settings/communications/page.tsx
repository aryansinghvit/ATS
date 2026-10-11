"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api/errors";
import {
  fetchCommunicationSettings,
  fetchTemplates,
  previewTemplate,
  resetTemplate,
  saveCommunicationSettings,
  updateTemplate,
} from "@/lib/api/workspace";
import type { CommunicationSettings, EmailTemplate } from "@/lib/types";

const VARIABLES = [
  "candidate_name",
  "candidate_first_name",
  "job_title",
  "company_name",
  "scheduling_link",
  "job_link",
  "stage_name",
];
const TIMEZONES = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
  "Europe/London",
  "Europe/Berlin",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Australia/Sydney",
];

function message(caught: unknown) {
  return caught instanceof ApiError ? caught.message : "Something went wrong.";
}

function TemplateEditor({ template, onSaved }: { template: EmailTemplate; onSaved: (t: EmailTemplate) => void }) {
  const [subject, setSubject] = useState(template.subject);
  const [body, setBody] = useState(template.body);
  const [enabled, setEnabled] = useState(template.enabled);
  const [preview, setPreview] = useState<{ subject: string; body: string } | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    setSubject(template.subject);
    setBody(template.body);
    setEnabled(template.enabled);
    setPreview(null);
    setStatus(null);
  }, [template]);

  const insert = (variable: string) => {
    const element = bodyRef.current;
    const token = `{{${variable}}}`;
    const start = element?.selectionStart ?? body.length;
    const end = element?.selectionEnd ?? body.length;
    setBody(body.slice(0, start) + token + body.slice(end));
    requestAnimationFrame(() => {
      element?.focus();
      element?.setSelectionRange(start + token.length, start + token.length);
    });
  };

  const run = async (action: () => Promise<void>) => {
    setStatus(null);
    try {
      await action();
    } catch (caught) {
      setStatus(message(caught));
    }
  };

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">{template.name}</h2>
        <p className="text-sm text-muted">
          {template.description} <span className="font-medium">Trigger: {template.trigger}.</span>
        </p>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled
      </label>
      <label className="block">
        <span className="text-sm font-medium">Subject</span>
        <input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-brand"
        />
      </label>
      <div>
        <span className="text-sm font-medium">Insert variable</span>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {VARIABLES.map((variable) => (
            <button
              key={variable}
              type="button"
              onClick={() => insert(variable)}
              className="rounded-full border border-border bg-surface px-2.5 py-0.5 font-mono text-xs hover:border-brand"
            >
              {`{{${variable}}}`}
            </button>
          ))}
        </div>
      </div>
      <textarea
        ref={bodyRef}
        rows={12}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        aria-label="Email body"
        className="w-full rounded-lg border border-border bg-surface px-3 py-2 font-mono text-sm outline-none focus:border-brand"
      />
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void run(async () => {
            onSaved(await updateTemplate(template.key, { subject, body, enabled }));
            setStatus("Saved.");
          })}
          className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-brand-contrast"
        >
          Save template
        </button>
        <button
          type="button"
          onClick={() => void run(async () => setPreview(await previewTemplate(template.key, subject, body)))}
          className="rounded-lg border border-border px-3 py-1.5 text-sm"
        >
          Preview
        </button>
        {template.customized && (
          <button
            type="button"
            onClick={() => void run(async () => {
              onSaved(await resetTemplate(template.key));
              setStatus("Restored the default template.");
            })}
            className="rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-surface-muted"
          >
            Reset to default
          </button>
        )}
      </div>
      {status && <p className="text-sm text-muted" role="status">{status}</p>}
      {preview && (
        <div className="rounded-xl border border-border bg-surface p-4">
          <p className="text-xs uppercase tracking-wider text-muted">Preview (sample candidate)</p>
          <p className="mt-1 font-semibold">{preview.subject}</p>
          <pre className="mt-2 whitespace-pre-wrap font-sans text-sm">{preview.body}</pre>
        </div>
      )}
    </div>
  );
}

function SettingsForm() {
  const [settings, setSettings] = useState<CommunicationSettings | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    fetchCommunicationSettings().then(setSettings).catch((caught) => setStatus(message(caught)));
  }, []);

  if (!settings) return <p className="text-sm text-muted">{status ?? "Loading settings…"}</p>;
  const save = async () => {
    try {
      setSettings(await saveCommunicationSettings({ ...settings, scheduling_url: settings.scheduling_url || null, reply_to: settings.reply_to || null }));
      setStatus("Settings saved.");
    } catch (caught) {
      setStatus(message(caught));
    }
  };

  return (
    <section className="space-y-4 rounded-2xl border border-border bg-surface p-4">
      <h2 className="font-semibold">Delivery settings</h2>
      <label className="block text-sm">
        <span className="font-medium">Scheduling link</span>
        <span className="block text-xs text-muted">Cal.com / Calendly URL injected into interview invitations as {"{{scheduling_link}}"}. A job&apos;s own link takes precedence.</span>
        <input
          type="url"
          placeholder="https://cal.com/your-team/interview"
          value={settings.scheduling_url ?? ""}
          onChange={(e) => setSettings({ ...settings, scheduling_url: e.target.value })}
          className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 outline-none focus:border-brand"
        />
      </label>
      <label className="block text-sm">
        <span className="font-medium">Polite rejection delay: {settings.rejection_delay_hours} hours</span>
        <input
          type="range"
          min={24}
          max={72}
          step={6}
          value={settings.rejection_delay_hours}
          onChange={(e) => setSettings({ ...settings, rejection_delay_hours: Number(e.target.value) })}
          className="mt-2 w-full accent-[var(--brand)]"
        />
        <span className="text-xs text-muted">Rejections are then moved into business hours (Mon–Fri 09:00–17:00).</span>
      </label>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-sm">
          <span className="font-medium">Timezone</span>
          <select
            value={settings.timezone}
            onChange={(e) => setSettings({ ...settings, timezone: e.target.value })}
            className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2"
          >
            {[settings.timezone, ...TIMEZONES.filter((tz) => tz !== settings.timezone)].map((tz) => (
              <option key={tz} value={tz}>{tz}</option>
            ))}
          </select>
        </label>
        <label className="block text-sm">
          <span className="font-medium">Reply-to address</span>
          <input
            type="email"
            value={settings.reply_to ?? ""}
            onChange={(e) => setSettings({ ...settings, reply_to: e.target.value })}
            className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 outline-none focus:border-brand"
          />
        </label>
      </div>
      <button type="button" onClick={() => void save()} className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-brand-contrast">
        Save settings
      </button>
      {status && <p className="text-sm text-muted" role="status">{status}</p>}
    </section>
  );
}

export default function CommunicationsSettingsPage() {
  const [templates, setTemplates] = useState<EmailTemplate[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTemplates()
      .then((data) => {
        setTemplates(data);
        setSelected(data[0]?.key ?? null);
      })
      .catch((caught) => setError(message(caught)));
  }, []);

  const onSaved = useCallback((updated: EmailTemplate) => {
    setTemplates((current) => current.map((t) => (t.key === updated.key ? updated : t)));
  }, []);

  const template = templates.find((t) => t.key === selected);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Candidate emails</h1>
        <p className="text-sm text-muted">Automated messages sent when candidates apply or move stages.</p>
      </header>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <SettingsForm />
      <div className="grid gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
        <nav aria-label="Email templates" className="space-y-1">
          {templates.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setSelected(t.key)}
              aria-current={t.key === selected}
              className={`block w-full rounded-lg px-3 py-2 text-left text-sm ${
                t.key === selected ? "bg-surface font-medium shadow-sm" : "text-muted hover:bg-surface"
              }`}
            >
              {t.name}
              <span className="block text-xs text-muted">
                {!t.enabled ? "Disabled" : t.customized ? "Customised" : "Default"}
              </span>
            </button>
          ))}
        </nav>
        {template && <TemplateEditor template={template} onSaved={onSaved} />}
      </div>
    </div>
  );
}
