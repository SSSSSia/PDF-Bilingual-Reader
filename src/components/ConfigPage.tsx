import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useConfigStore, SectionTest } from "../stores/configStore";
import { AppConfig } from "../types";
import { testApiConnection } from "../lib/bridge";
import LoadingSpinner from "./common/LoadingSpinner";

/**
 * API 配置页 —— 交互逻辑参照 CadAgent 的 API 管理：
 * 1. 提供商「预设」下拉：选中只填充 API 地址（既定决策 ：
 * 预设不改模型名称，模型始终用户自选；Custom 不覆盖）；
 * 2. 地址 / Key / 模型始终可编辑，Key 带显隐切换；
 * 3. 「测试连接」经本地后端代理发最小请求，内联回显结果；
 * 4. 加载时按 地址 反推预设，匹配不上落「自定义」。
 */

interface Preset {
  name: string;
  url: string;
  model: string;
  provider: string;
}

// 翻译预设（全部为 OpenAI 兼容 /chat/completions 服务；model 仅作占位不再下发）
const TRANSLATE_PRESETS: Preset[] = [
  { name: "SiliconFlow", url: "https://api.siliconflow.cn/v1", model: "deepseek-ai/DeepSeek-V4-Flash", provider: "openai" },
  { name: "DeepSeek", url: "https://api.deepseek.com/v1", model: "deepseek-chat", provider: "openai" },
  { name: "智谱AI", url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-plus", provider: "openai" },
  { name: "通义千问", url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus", provider: "openai" },
  { name: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-4o-mini", provider: "openai" },
  { name: "Moonshot", url: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k", provider: "openai" },
  { name: "本地 Ollama", url: "http://localhost:11434/v1", model: "qwen3:8b", provider: "openai" },
  { name: "自定义", url: "", model: "", provider: "openai" },
];

// OCR 预设（仅收录支持视觉输入的服务，避免"选了必失败"的预设）
const OCR_PRESETS: Preset[] = [
  { name: "SiliconFlow", url: "https://api.siliconflow.cn/v1", model: "PaddlePaddle/PaddleOCR-VL-1.5", provider: "openai" },
  { name: "智谱AI", url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4v-plus", provider: "openai" },
  { name: "通义千问", url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-vl-plus", provider: "openai" },
  { name: "OpenAI", url: "https://api.openai.com/v1", model: "gpt-4o", provider: "openai" },
  { name: "本地 Ollama", url: "http://localhost:11434/v1", model: "llava", provider: "openai" },
  { name: "自定义", url: "", model: "", provider: "openai" },
];

/** API Key 输入：密码态 + 显隐切换（切换钮内嵌输入框右缘，一体成形） */
function PasswordField({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative">
      <input
        id={id}
        type={show ? "text" : "password"}
        className="input-field pr-16"
        placeholder="sk-..."
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        aria-label={show ? "隐藏 API Key" : "显示 API Key"}
        title={show ? "隐藏" : "显示"}
        className="absolute inset-y-1 right-1 my-auto flex h-8 w-14 items-center justify-center rounded-md text-xs font-medium text-slate-500 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-200"
      >
        {show ? "隐藏" : "显示"}
      </button>
    </div>
  );
}

const sameSpec = (
  a: SectionTest["spec"],
  b: { api_url: string; api_key: string; model: string; mode: "ocr" | "text" },
) => !!a && a.api_url === b.api_url && a.api_key === b.api_key && a.model === b.model && a.mode === b.mode;

interface ApiSectionProps {
  title: string;
  value: { provider: string; api_key: string; api_url: string; model: string };
  presets: Preset[];
  onChange: (field: "provider" | "api_key" | "api_url" | "model", v: string) => void;
  testMode: "ocr" | "text";
  test: SectionTest;
  onTest: () => void;
  children?: React.ReactNode;
}

/** 单个 API 区块：预设下拉 + 地址/Key/模型 + 测试连接 */
function ApiSection({ title, value, presets, onChange, testMode, test, onTest, children }: ApiSectionProps) {
  // 只按地址反推预设
  const matched = presets.findIndex((p) => p.url === value.api_url && p.url);
  const presetIndex = matched >= 0 ? matched : presets.length - 1;

  const handlePreset = (idx: number) => {
    const preset = presets[idx];
    if (!preset || preset.name === "自定义") return; // Custom 不覆盖现有值
    onChange("api_url", preset.url);
    onChange("provider", preset.provider);
    // 模型不随预设切换
  };

  const urlId = `${testMode}-url`;
  const keyId = `${testMode}-key`;
  const modelId = `${testMode}-model`;

  return (
    <section className="card p-5">
      <h2 className="mb-4 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {title}
      </h2>
      <div className="space-y-4">
        <div>
          <label className="form-label" htmlFor={`${testMode}-preset`}>
            提供商预设
          </label>
          <select
            id={`${testMode}-preset`}
            className="input-field"
            value={presetIndex}
            onChange={(e) => handlePreset(Number(e.target.value))}
          >
            {presets.map((p, i) => (
              <option key={p.name} value={i}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="form-label" htmlFor={urlId}>
            API 地址
          </label>
          <input
            id={urlId}
            className="input-field"
            placeholder="https://api.example.com/v1"
            value={value.api_url}
            onChange={(e) => onChange("api_url", e.target.value)}
          />
        </div>
        <div>
          <label className="form-label" htmlFor={keyId}>
            API Key
          </label>
          <PasswordField
            id={keyId}
            value={value.api_key}
            onChange={(v) => onChange("api_key", v)}
          />
        </div>
        <div>
          <label className="form-label" htmlFor={modelId}>
            模型名称
          </label>
          <input
            id={modelId}
            className="input-field"
            placeholder="model-name"
            value={value.model}
            onChange={(e) => onChange("model", e.target.value)}
          />
        </div>
        {children}
        <div className="flex items-center justify-between gap-3 pt-1">
          <button
            type="button"
            onClick={onTest}
            disabled={test.state === "testing" || !value.api_url.trim()}
            className="btn-secondary"
          >
            {test.state === "testing" ? "测试中…" : "测试连接"}
          </button>
          {test.state === "ok" && (
            <span
              role="status"
              className="animate-fade-in text-sm font-medium text-emerald-600 dark:text-emerald-400"
            >
              ✓ {test.msg}
            </span>
          )}
          {test.state === "fail" && (
            <span
              role="alert"
              className="animate-fade-in min-w-0 flex-1 truncate text-right text-sm text-red-600 dark:text-red-400"
              title={test.msg}
            >
              ✗ {test.msg}
            </span>
          )}
        </div>
      </div>
    </section>
  );
}

export default function ConfigPage() {
  const { config, saveConfig, loadConfig } = useConfigStore();
  const navigate = useNavigate();
  const [form, setForm] = useState<AppConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 两个区块的测试状态（全局 store 持有：保存触发的自动补测若被切页打断，
  // ✓/✗ 结果不随组件卸载丢失，返回设置页仍可见）
  const tests = useConfigStore((s) => s.apiTests);
  const setApiTest = useConfigStore((s) => s.setApiTest);

  /** 返回目标：固定回主页（文献库）。
   * 页面逻辑重规划：导航层级为 文献库 ← 阅读页/设置页，
   * pdfStore 的 pages 在返回主页后仍驻留内存，若按「有结果就回阅读页」
   * 判断，用户从主页进设置点返回会被弹回阅读页（返回循环 bug），
   * 故不再依赖内存状态判断。 */
  const backTarget = "/";

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  useEffect(() => {
    if (config) setForm(config);
  }, [config]);

  if (!form) return <LoadingSpinner />;

  const sectionOf = (m: "ocr" | "text") =>
    m === "ocr" ? form.ocr : form.translate;

  /** 手动点「测试连接」或保存时自动补测共用；结果内联回显在对应区块 */
  const runTest = async (m: "ocr" | "text") => {
    const cfg = sectionOf(m);
    if (!cfg.api_url.trim()) return;
    const spec = {
      api_url: cfg.api_url,
      api_key: cfg.api_key,
      model: cfg.model,
      mode: m,
    };
    setApiTest(m, { state: "testing", msg: "" });
    try {
      const r = await testApiConnection(spec);
      setApiTest(m, {
        state: "ok",
        msg: `连接成功（${r.model || cfg.model || "未知模型"}）`,
        spec,
      });
    } catch (e) {
      setApiTest(m, {
        state: "fail",
        msg: e instanceof Error ? e.message : String(e),
        spec,
      });
    }
  };

  const updateField = (section: string, field: string, value: string) => {
    setForm((prev) =>
      prev
        ? {
            ...prev,
            [section]: { ...prev[section as keyof AppConfig], [field]: value },
          }
        : prev
    );
  };

  const handleSave = async () => {
    if (!form) return;
    // 保存前校验（参考 CadAgent：地址/模型必填，Key 允许为空以兼容本地 Ollama）
    if (!form.ocr.api_url.trim() || !form.ocr.model.trim()) {
      setError("OCR 服务需填写 API 地址与模型名称");
      return;
    }
    if (!form.translate.api_url.trim() || !form.translate.model.trim()) {
      setError("翻译服务需填写 API 地址与模型名称");
      return;
    }
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await saveConfig(form);
    } catch (e) {
      console.error("Save failed:", e);
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
      return;
    }
    // 既定决策：保存时自动补测「在当前表单值下还没测成功过」的
    // 服务（结果回显在各区块的测试按钮旁）；两个服务都已测成功则直接保存，
    // 不再重复请求。测试结果不阻塞保存（本地 Ollama 离线也允许保存）。
    const needTest = (["ocr", "text"] as const).filter((m) => {
      const cfg = sectionOf(m);
      const t = tests[m];
      return !(
        t.state === "ok" &&
        sameSpec(t.spec, {
          api_url: cfg.api_url,
          api_key: cfg.api_key,
          model: cfg.model,
          mode: m,
        })
      );
    });
    if (needTest.length > 0) {
      await Promise.all(needTest.map((m) => runTest(m)));
    }
    setSaved(true);
    setSaving(false);
  };

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="mb-5 text-lg font-semibold text-slate-900 dark:text-slate-100">
        API 配置
      </h1>

      <div className="space-y-5">
        <ApiSection
          title="OCR 服务（图片识别）"
          value={form.ocr}
          presets={OCR_PRESETS}
          onChange={(field, v) => updateField("ocr", field, v)}
          testMode="ocr"
          test={tests.ocr}
          onTest={() => void runTest("ocr")}
        />

        <ApiSection
          title="翻译服务"
          value={form.translate}
          presets={TRANSLATE_PRESETS}
          onChange={(field, v) => updateField("translate", field, v)}
          testMode="text"
          test={tests.text}
          onTest={() => void runTest("text")}
        >
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label className="form-label" htmlFor="tr-source">
                源语言
              </label>
              <select
                id="tr-source"
                className="input-field"
                value={form.translate.source_language === "zh" ? "zh" : "en"}
                onChange={(e) => {
                  const v = e.target.value;
                  updateField("translate", "source_language", v);
                  // 只允许中英互译：源语言变动时，目标语言自动翻转为另一种
                  if (form.translate.target_language === v) {
                    updateField("translate", "target_language", v === "zh" ? "en" : "zh");
                  }
                }}
              >
                <option value="zh">中文</option>
                <option value="en">English</option>
              </select>
            </div>
            <div>
              <label className="form-label" htmlFor="tr-target">
                目标语言
              </label>
              <select
                id="tr-target"
                className="input-field"
                value={form.translate.target_language === "zh" ? "zh" : "en"}
                onChange={(e) => {
                  const v = e.target.value;
                  updateField("translate", "target_language", v);
                  if (form.translate.source_language === v) {
                    updateField("translate", "source_language", v === "zh" ? "en" : "zh");
                  }
                }}
              >
                <option value="zh">中文</option>
                <option value="en">English</option>
              </select>
            </div>
          </div>
        </ApiSection>

        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(backTarget)}
            disabled={saving}
            className="btn-secondary"
            title="返回文献库"
          >
            ← 返回
          </button>
          <button onClick={handleSave} disabled={saving} className="btn-primary">
            {saving ? "保存中..." : "保存配置"}
          </button>
          {saved && (
            <span
              role="status"
              className="animate-fade-in text-sm font-medium text-emerald-600 dark:text-emerald-400"
            >
              ✓ 已保存
            </span>
          )}
          {error && (
            <span
              role="alert"
              className="animate-fade-in text-sm text-red-600 dark:text-red-400"
            >
              {error}
            </span>
          )}
        </div>

        {/* AGPL 合规一行声明（应用无独立关于页，设置页为唯一落点） */}

        <p className="text-xs leading-relaxed text-slate-400 dark:text-slate-500">
          「原版PDF·左右对照」排版对照功能基于开源项目 BabelDOC（AGPL-3.0，版本
          0.6.4）经独立子进程调用实现，版权归 funstory-ai 及 BabelDOC 原作者所有。
        </p>
      </div>
    </div>
  );
}
