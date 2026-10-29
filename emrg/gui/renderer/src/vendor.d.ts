/**
 * vendor.d.ts — vendor/ 目录下 UMD 文件的模块声明（TS 无声明文件）。
 * marked/dompurify 以 default import 进入 bundle；highlight.custom.js 仅副作用
 * 导入（运行时写 window.hljs），无需导出声明。
 */
declare module "*/vendor/marked.min.js" {
  interface MarkedLike {
    parse(text: string, opts?: Record<string, unknown>): string | Promise<string>;
    lexer(text: string): Array<Record<string, unknown>>;
    parser(tokens: Array<Record<string, unknown>>): string;
    use(opts: {
      renderer?: {
        code?: (code: string, infostring: string, escaped: boolean) => string;
        image?: (href: string, title: string | null, text: string) => string;
      };
    }): unknown;
  }
  const marked: MarkedLike;
  export default marked;
}

declare module "*/vendor/dompurify.min.js" {
  interface DomPurifyLike {
    sanitize(html: string): string;
    /** DOMPurify addHook —— 放行 img[src] 的 file: 只有这一条路（issue #2638） */
    addHook?(name: string, fn: (node: { nodeName?: unknown }, data: { attrName?: unknown; attrValue?: unknown; forceKeepAttr?: unknown }) => void): void;
  }
  const DOMPurify: DomPurifyLike;
  export default DOMPurify;
}
