import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useModels } from "../../hooks/useModels";
import { useApi } from "../../api/context";
import { isValidKubernetesName } from "../../lib/format";
import { SectionLabel } from "../../components/SectionLabel";
import { Button } from "../../components/Button";
import { Modal } from "../../components/Modal";
import type { ClusterModel } from "../../api/types";

const labelClass =
  "block font-mono text-[11px] uppercase tracking-wider text-muted mb-1";
const inputClass =
  "bg-bg-mid border border-border rounded-lg px-3 py-2 text-text w-full focus:outline-none focus:border-border-hi";

interface FormState {
  name: string;
  modelName: string;
  repo: string;
  backend: string;
  engine: string;
  vram: string;
  image: string;
  args: string;
}

const emptyForm: FormState = {
  name: "",
  modelName: "",
  repo: "",
  backend: "KServe",
  engine: "vLLM",
  vram: "",
  image: "",
  args: "",
};

function RegisterModelModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [form, setForm] = useState<FormState>(emptyForm);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nameBlurred, setNameBlurred] = useState(false);

  const nameInvalid = nameBlurred && form.name !== "" && !isValidKubernetesName(form.name);

  const set = (field: keyof FormState) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
  ) => setForm((prev) => ({ ...prev, [field]: e.target.value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const args = form.args
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const profileName = `${form.engine.toLowerCase()}-${form.backend.toLowerCase()}`;
    const cm: Partial<ClusterModel> = {
      apiVersion: "modelplane.ai/v1alpha1",
      kind: "ClusterModel",
      metadata: { name: form.name },
      spec: {
        model: { name: form.modelName },
        source: "HuggingFace",
        huggingFace: { repo: form.repo },
        resources: { vram: form.vram },
        serving: [
          {
            name: profileName,
            backend: form.backend,
            engine: {
              name: form.engine,
              image: form.image || `vllm/vllm-openai:latest`,
              ...(args.length ? { args } : {}),
            },
          },
        ],
      },
    };

    try {
      await api.createClusterModel(cm);
      await queryClient.invalidateQueries({ queryKey: ["clustermodels"] });
      setForm(emptyForm);
      setNameBlurred(false);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create model");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Register Model">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className={labelClass}>Name</label>
          <input
            type="text"
            required
            value={form.name}
            onChange={set("name")}
            onBlur={() => setNameBlurred(true)}
            placeholder="qwen2-0.5b-instruct"
            className={inputClass}
          />
          {nameInvalid && (
            <p className="text-xs text-red mt-1">
              Invalid Kubernetes name. Must be lowercase alphanumeric or hyphens, and start/end with an alphanumeric character.
            </p>
          )}
        </div>

        <div>
          <label className={labelClass}>Model Name</label>
          <input
            type="text"
            required
            value={form.modelName}
            onChange={set("modelName")}
            placeholder="Qwen/Qwen2.5-0.5B-Instruct"
            className={inputClass}
          />
        </div>

        <div>
          <label className={labelClass}>HuggingFace Repo</label>
          <input
            type="text"
            required
            value={form.repo}
            onChange={set("repo")}
            placeholder="Qwen/Qwen2.5-0.5B-Instruct"
            className={inputClass}
          />
        </div>

        <div>
          <label className={labelClass}>VRAM</label>
          <input
            type="text"
            required
            value={form.vram}
            onChange={set("vram")}
            placeholder="2Gi"
            className={inputClass}
          />
        </div>

        <SectionLabel>SERVING PROFILE</SectionLabel>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelClass}>Backend</label>
            <select value={form.backend} onChange={set("backend")} className={inputClass}>
              <option value="KServe">KServe</option>
              <option value="Dynamo">Dynamo</option>
            </select>
          </div>
          <div>
            <label className={labelClass}>Engine</label>
            <select value={form.engine} onChange={set("engine")} className={inputClass}>
              <option value="vLLM">vLLM</option>
              <option value="SGLang">SGLang</option>
            </select>
          </div>
        </div>

        <div>
          <label className={labelClass}>Image</label>
          <input
            type="text"
            value={form.image}
            onChange={set("image")}
            placeholder="vllm/vllm-openai:v0.7.3"
            className={inputClass}
          />
        </div>

        <div>
          <label className={labelClass}>Args (optional, comma-separated)</label>
          <input
            type="text"
            value={form.args}
            onChange={set("args")}
            placeholder="--max-model-len=4096, --enable-prefix-caching"
            className={inputClass}
          />
        </div>

        {error && <p className="text-sm text-red">{error}</p>}

        <div className="flex items-center justify-end gap-3 pt-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={submitting || nameInvalid}>
            {submitting ? "Registering…" : "Register"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function CatalogPage() {
  const { data, isLoading, error } = useModels();
  const api = useApi();
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [deleting, setDeleting] = useState<Set<string>>(new Set());

  const handleDelete = async (name: string) => {
    setDeleting((prev) => new Set(prev).add(name));
    try {
      await api.deleteClusterModel(name);
      await queryClient.invalidateQueries({ queryKey: ["clustermodels"] });
    } finally {
      setDeleting((prev) => {
        const next = new Set(prev);
        next.delete(name);
        return next;
      });
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-muted">
        Loading models…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center py-20 text-red">
        Failed to load models:{" "}
        {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const models = data?.items ?? [];

  return (
    <div>
      <div className="flex items-start justify-between mb-4">
        <div>
          <SectionLabel>Model Catalog</SectionLabel>
          <p className="text-sm text-muted -mt-2">
            Manage the models available for deployment
          </p>
        </div>
        <Button onClick={() => setModalOpen(true)}>Register Model</Button>
      </div>

      <table className="w-full mt-4">
        <thead>
          <tr className="font-mono text-[11px] uppercase tracking-wider text-muted">
            <th className="text-left px-4 py-2 font-normal">Name</th>
            <th className="text-left px-4 py-2 font-normal">Model</th>
            <th className="text-left px-4 py-2 font-normal">Backends</th>
            <th className="text-left px-4 py-2 font-normal">VRAM</th>
            <th className="text-right px-4 py-2 font-normal" />
          </tr>
        </thead>
        <tbody>
          {models.length === 0 && (
            <tr>
              <td
                colSpan={5}
                className="px-4 py-8 text-center text-sm text-muted"
              >
                No models registered
              </td>
            </tr>
          )}
          {models.map((m) => {
            const name = m.metadata.name;
            return (
              <tr key={name} className="border-b border-border">
                <td className="px-4 py-3 text-sm text-text">{name}</td>
                <td className="px-4 py-3 text-sm text-muted-hi">
                  {m.spec.model.name}
                </td>
                <td className="px-4 py-3 text-sm">
                  <div className="flex flex-wrap gap-1">
                    {(m.spec.serving ?? []).map((p) => (
                      <span key={p.name} className="text-xs font-mono text-cyan bg-cyan/10 px-1.5 py-0.5 rounded">
                        {p.engine?.name ?? p.backend}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-4 py-3 text-sm font-mono text-muted-hi">
                  {m.spec.resources.vram}
                </td>
                <td className="px-4 py-3 text-right">
                  <Button
                    variant="danger"
                    className="text-xs px-2 py-1"
                    disabled={deleting.has(name)}
                    onClick={() => handleDelete(name)}
                  >
                    {deleting.has(name) ? "Deleting…" : "Delete"}
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <RegisterModelModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
      />
    </div>
  );
}
