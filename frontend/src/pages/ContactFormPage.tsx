import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router";

import { Card, PageHeader, QueryState, SelectField, TextArea } from "../components/common";
import { Alert, Button, TextField } from "../components/ui";
import {
  CONTACT_SOURCES,
  CONTACT_STATUSES,
  crm,
  label,
  type Contact,
  type ContactInput,
} from "../lib/crm";
import { errorMessage } from "../lib/errors";
import { validateContact, type ContactFormValues as FormValues } from "../lib/contactForm";
import { useMembers } from "../lib/members";


function toValues(c?: Contact): FormValues {
  return {
    name: c?.name ?? "",
    organization: c?.organization ?? "",
    phone: c?.phone ?? "",
    email: c?.email ?? "",
    designation: c?.designation ?? "",
    status: c?.status ?? "NEW",
    source: c?.source ?? "MANUAL",
    tags: c?.tags.join(", ") ?? "",
    notes: c?.notes ?? "",
    owner_user_id: c ? (c.owner_user_id ?? "") : "__me__",
  };
}

export function ContactFormPage() {
  const { id } = useParams();
  const existing = useQuery({
    queryKey: ["contact", id],
    queryFn: () => crm.getContact(id!),
    enabled: Boolean(id),
  });
  if (id) {
    return (
      <QueryState isPending={existing.isPending} error={existing.error}>
        {existing.data ? <ContactForm contact={existing.data} /> : null}
      </QueryState>
    );
  }
  return <ContactForm />;
}

function ContactForm({ contact }: { contact?: Contact }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const members = useMembers();
  const [values, setValues] = useState<FormValues>(() => toValues(contact));
  const [errors, setErrors] = useState<Partial<Record<keyof FormValues, string>>>({});

  const save = useMutation({
    mutationFn: (body: ContactInput) =>
      contact ? crm.updateContact(contact.id, body) : crm.createContact(body),
    onSuccess: (saved) => {
      queryClient.invalidateQueries({ queryKey: ["contacts"] });
      queryClient.setQueryData(["contact", saved.id], saved);
      queryClient.invalidateQueries({ queryKey: ["timeline", saved.id] });
      navigate(`/contacts/${saved.id}`);
    },
  });

  const set = (field: keyof FormValues) => (e: { target: { value: string } }) =>
    setValues((v) => ({ ...v, [field]: e.target.value }));

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const found = validateContact(values);
    setErrors(found);
    if (Object.keys(found).length) return;
    const body: ContactInput = {
      name: values.name.trim(),
      organization: values.organization.trim() || null,
      phone: values.phone.trim() || null,
      email: values.email.trim() || null,
      designation: values.designation.trim() || null,
      status: values.status as Contact["status"],
      source: values.source as Contact["source"],
      tags: values.tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      notes: values.notes.trim() || null,
    };
    if (values.owner_user_id !== "__me__") body.owner_user_id = values.owner_user_id || null;
    save.mutate(body);
  }

  const ownerOptions = [
    ...(contact ? [] : [{ value: "__me__", label: "Me" }]),
    { value: "", label: "Unassigned" },
    ...(members.data?.items.map((m) => ({ value: m.user_id, label: m.full_name })) ?? []),
  ];

  return (
    <div className="mx-auto max-w-2xl">
      <PageHeader title={contact ? `Edit ${contact.name}` : "Add contact"} />
      <Card>
        <form className="grid grid-cols-1 gap-4 sm:grid-cols-2" onSubmit={onSubmit} noValidate>
          {save.error ? (
            <div className="sm:col-span-2">
              <Alert>{errorMessage(save.error)}</Alert>
            </div>
          ) : null}
          <TextField label="Name" value={values.name} onChange={set("name")} error={errors.name} />
          <TextField label="Company" value={values.organization} onChange={set("organization")} />
          <TextField label="Phone" value={values.phone} onChange={set("phone")} error={errors.phone} />
          <TextField
            label="Email"
            type="email"
            value={values.email}
            onChange={set("email")}
            error={errors.email}
          />
          <TextField label="Designation" value={values.designation} onChange={set("designation")} />
          <SelectField
            label="Status"
            value={values.status}
            onChange={set("status")}
            options={CONTACT_STATUSES.map((s) => ({ value: s, label: label(s) }))}
          />
          <SelectField
            label="Source"
            value={values.source}
            onChange={set("source")}
            options={CONTACT_SOURCES.map((s) => ({ value: s, label: label(s) }))}
          />
          <SelectField
            label="Owner"
            value={values.owner_user_id}
            onChange={set("owner_user_id")}
            options={ownerOptions}
          />
          <div className="sm:col-span-2">
            <TextField
              label="Tags"
              hint="Comma separated, e.g. vip, north"
              value={values.tags}
              onChange={set("tags")}
            />
          </div>
          <div className="sm:col-span-2">
            <TextArea label="Notes" value={values.notes} onChange={set("notes")} />
          </div>
          <div className="flex gap-2 sm:col-span-2">
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
            <Button type="button" variant="secondary" onClick={() => navigate(-1)}>
              Cancel
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
