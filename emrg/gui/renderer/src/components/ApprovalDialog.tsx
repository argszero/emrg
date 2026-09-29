import { useI18n } from "../lib/i18n";
import type { PendingApproval } from "../lib/daemonBridge";
import { Dialog } from "./Dialog";

/**
 * ApprovalDialog — daemon 的提权提问（rant 2026-09-29T15:52:38.987951+08:00，要求 1）。
 *
 * 这不是一条可忽略的通知：daemon 正**阻塞**在这个问题上 —— 一条被要求在其上运行
 * 的受限命令在等答复，而「没有任何答复」按 fail-closed 规则就是拒绝。因此：
 *
 * - **只有两个出口，且两条都作答**：批准 / 拒绝，以及 ESC（= 拒绝）。组件没有
 *   「稍后再说」的第三条路 —— 静默地与同意是不同的两件事，而这里最坏的一种解读
 *   必须不可能发生：不回答永远不会变成批准（daemon 侧超时同样落到拒绝）。
 * - 提问文本原样显示（daemon 已经把它写成多行：从哪一档、去哪一档、
 *   justification），所以用 `pre-wrap` 保留换行，不在这里重新排版 —— 重新排版会
 *   让显示的内容与 daemon 实际问的内容出现第二份事实。
 */
export interface ApprovalDialogProps {
  /** daemon 正在等答复的提问；null = 没有未决提问。 */
  request: PendingApproval | null;
  /** 作答（true=批准，false=拒绝）。作答后对话框即关闭。 */
  onAnswer: (approved: boolean) => void;
}

export function ApprovalDialog({ request, onAnswer }: ApprovalDialogProps) {
  const { t } = useI18n();
  if (!request) return null;

  // ESC / 关闭等同于拒绝：daemon 侧的超时也已经落到拒绝，所以这是同一个答案的
  // 两种到达方式，而不是一个额外的「不理它」。组件不持有自己的可见状态 ——
  // 提问是否还在屏幕上由 store 决定，作答即清空（调用方的 respondApproval）。
  return (
    <Dialog
      open
      title={t("approval.title")}
      onClose={() => onAnswer(false)}
      testId="approval-dialog"
      actions={
        <>
          <button
            type="button"
            className="btn btn-ghost"
            data-testid="approval-deny"
            onClick={() => onAnswer(false)}
          >
            {t("approval.deny")}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="approval-allow"
            onClick={() => onAnswer(true)}
          >
            {t("approval.allow")}
          </button>
        </>
      }
    >
      <p
        className="approval-question"
        data-testid="approval-question"
        style={{
          color: "var(--text-2)",
          fontSize: "var(--fs-secondary)",
          whiteSpace: "pre-wrap",
        }}
      >
        {request.question}
      </p>
      <p
        className="approval-hint"
        data-testid="approval-hint"
        style={{ color: "var(--text-3)", fontSize: "var(--fs-secondary)" }}
      >
        {t("approval.silenceIsDenial")}
      </p>
    </Dialog>
  );
}
