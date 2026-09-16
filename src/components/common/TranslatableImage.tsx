import { useState } from "react";
import MarkdownText from "./MarkdownText";
import { translateFigureImage } from "../../lib/bridge";

/** 表格快照的图片路径标记（提取时表格命名 tab_*，图命名 fig_*） */
const TABLE_IMG = /\/tab_p\d+_\d+\.png/;
/** 已生成的译制图（不再显示按钮） */
const ZH_IMG = /\.zh\.v\d+\.png/;

/**
 * 可按需翻译的表格图片：
 * 全文翻译不等待表格；表格图右上角常驻「译」按钮，点击后单独调后端
 * 生成译制图（原排版+图内文字译文），就地替换显示。图（fig_*）不显示按钮。
 * 生成结果后端有文件级缓存，重复点击幂等。
 * interactive=false 时不显示按钮。
 */
export default function TranslatableImage({
  md,
  interactive = true,
}: {
  md: string;
  interactive?: boolean;
}) {
  const [text, setText] = useState(md);
  const [state, setState] = useState<"idle" | "busy" | "err" | "done">("idle");
  const [errMsg, setErrMsg] = useState("");
  const isTable = TABLE_IMG.test(md) || TABLE_IMG.test(text);
  const alreadyZh = ZH_IMG.test(text);

  if (!isTable) {
    return <MarkdownText text={text} />;
  }

  const handle = async () => {
    if (state === "busy") return;
    setState("busy");
    setErrMsg("");
    try {
      // 从当前 markdown 中提取快照路径
      const m = text.match(/!\[[^\]]*\]\(([^)]+)\)/);
      const path = m ? m[1] : "";
      const translated = await translateFigureImage(path);
      setText(translated);
      setState("done");
    } catch (e) {
      console.error("表格译制失败:", e);
      setErrMsg(e instanceof Error ? e.message : String(e));
      setState("err");
    }
  };

  return (
    <div className="relative">
      <MarkdownText text={text} />
      {interactive && state !== "done" && !alreadyZh && (
        <button
          type="button"
          onClick={handle}
          disabled={state === "busy"}
          title={
            state === "err"
              ? "生成失败，点击重试"
              : "生成译制图：保留表格排版，翻译表内文字"
          }
          className={`absolute right-3 -top-3 rounded-md border px-2.5 py-1 text-xs shadow-sm transition-colors ${
            state === "err"
              ? "border-red-300 bg-red-50 text-red-600 hover:bg-red-100 dark:border-red-800 dark:bg-red-950 dark:text-red-400"
              : "border-slate-300 bg-white/90 text-slate-600 hover:border-blue-400 hover:text-blue-600 dark:border-slate-600 dark:bg-slate-900/90 dark:text-slate-300 dark:hover:text-blue-400"
          } disabled:cursor-wait disabled:opacity-60`}
        >
          {state === "busy" ? "翻译中…" : state === "err" ? "重试" : "译"}
        </button>
      )}
      {state === "err" && errMsg && (
        <div className="mt-1 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-600 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
          译制图生成失败：{errMsg}
          {/后端请求失败/.test(errMsg) && "（若持续出现，请确认后端已重启为最新代码）"}
        </div>
      )}
    </div>
  );
}
