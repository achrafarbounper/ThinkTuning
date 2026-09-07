/**
 * markdown.tsx — Rendu Markdown des réponses de l'assistant (zéro dépendance).
 *
 * Historique UX : la bulle de l'assistant affichait le texte brut du modèle —
 * les titres « ## », le gras « **texte** », les listes et les blocs de code
 * apparaissaient littéralement, seuls les sauts de ligne étaient préservés
 * (white-space: pre-wrap). Ce module convertit le Markdown émis par
 * l'assistant en éléments React natifs, sans jamais modifier le contenu.
 *
 * Sécurité : aucun innerHTML / dangerouslySetInnerHTML — tous les nœuds sont
 * des éléments React, donc échappés par construction. Les liens sont restreints
 * aux schèmes http/https/mailto ; tout autre schème (javascript:, data:…) est
 * rendu littéral, jamais interprété.
 *
 * Streaming : le parseur est tolérant aux flux incomplets — un « ** » ou une
 * fence « ``` » non fermés restent littéraux / se ferment implicitement en fin
 * de flux au lieu de casser la bulle.
 *
 * Sous-ensemble supporté (suffisant pour un rapport) :
 *   - Titres ATX « # » à « ###### » (les « # » de fin sont ignorés) ;
 *   - Gras **…** / __…__, italique *…* / _…_, barré ~~…~~, code `…` ;
 *   - Blocs de code fences ``` avec label de langage ;
 *   - Listes à puces (-, *, +), ordonnées (1. / 1)), imbriquées par
 *     indentation, cases à cocher GFM (« - [ ] » / « - [x] ») ;
 *   - Citations « > » (récursives), séparateurs --- / *** / ___ ;
 *   - Tableaux GFM simples avec alignements (| :--- | :---: | ---: |) ;
 *   - Liens [texte](url) ;
 *   - Saut de ligne simple rendu <br/> (confort chat, plus permissif que la
 *     règle GitHub qui exige deux espaces ou une ligne vide).
 *
 * Limites assumées : setext headings (=== / --- sous un paragraphe), HTML
 * brut, autolinks <http://…>, images et échappements « \| » dans les
 * tableaux ne sont pas supportés (hors périmètre des réponses de l'agent).
 *
 * Évolution : si les besoins dépassent ce sous-ensemble, migrer vers
 * react-markdown + remark-gfm derrière ce même composant MarkdownContent.
 */

import { Fragment } from "react";
import type { CSSProperties, ReactNode } from "react";

/* =============================== Types ================================== */

/** Élément brut de liste avant construction de l'arbre d'imbrication. */
interface RawItem {
  indent: number;
  marker: string;
  text: string;
  task: boolean;
  checked: boolean;
}

/** Nœud de liste (récursif : children = sous-liste imbriquée). */
interface ListNode {
  text: string;
  task: boolean;
  checked: boolean;
  marker: string;
  children: ListNode[];
}

type TableAlign = "left" | "center" | "right";

type Block =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; lines: string[] }
  | { type: "code"; lang: string; text: string }
  | { type: "quote"; children: Block[] }
  | { type: "list"; items: ListNode[] }
  | { type: "hr" }
  | { type: "table"; header: string[]; align: Array<TableAlign | null>; rows: string[][] };

/* ============================ Expressions =============================== */

const FENCE_RE = /^ {0,3}```\s*(\S*)\s*$/;
const HEADING_RE = /^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$/;
const HR_RE = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$/;
const QUOTE_RE = /^ {0,3}>[ \t]?(.*)$/;
const LIST_RE = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const TASK_RE = /^\[([ xX])\][ \t]+(.*)$/;
const TABLE_SEP_RE = /^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$/;

const CODE_SPAN_RE = /^`([^`\n]+)`/;
const BOLD_RE = /^\*\*(.+?)\*\*/;
const BOLD_US_RE = /^__(.+?)__/;
const STRIKE_RE = /^~~(.+?)~~/;
const LINK_RE = /^\[([^\]\n]+)\]\(([^()\s]+)(?:[ \t]+"[^"]*")?\)/;
const EM_RE = /^\*(?![ \t*])([^*\n]+?)\*/;
const EM_US_RE = /^_(?![ \t_])([^_\n]+?)_(?![A-Za-z0-9])/;
const SAFE_URL_RE = /^(https?:\/\/|mailto:)/i;
const WORD_CHAR_RE = /[A-Za-z0-9_]/;

/* ============================== Parseur ================================= */

function isBlank(line: string): boolean {
  return line.trim() === "";
}

/** Un « _ » / « __ » collé à un caractère de mot reste littéral (snake_case…). */
function wordCharBefore(text: string, index: number): boolean {
  return index > 0 && WORD_CHAR_RE.test(text.charAt(index - 1));
}

/** Début d'un nouveau bloc : termine paragraphe / continuation de citation. */
function isBlockStart(line: string): boolean {
  return (
    FENCE_RE.test(line) ||
    HEADING_RE.test(line) ||
    HR_RE.test(line) ||
    QUOTE_RE.test(line) ||
    LIST_RE.test(line)
  );
}

function splitTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((cell) => cell.trim());
}

function separatorAlign(cell: string): TableAlign | null {
  const c = cell.trim();
  const left = c.startsWith(":");
  const right = c.endsWith(":");
  if (left && right) return "center";
  if (right) return "right";
  if (left) return "left";
  return null;
}

function collectListItems(lines: string[], start: number): { items: RawItem[]; next: number } {
  const first = LIST_RE.exec(lines[start]);
  if (!first) return { items: [], next: start };
  const baseIndent = first[1].length;
  const baseOrdered = /^\d/.test(first[2]);
  const items: RawItem[] = [];
  let i = start;
  let sawBlank = false;
  while (i < lines.length) {
    if (isBlank(lines[i])) {
      sawBlank = true;
      let j = i + 1;
      while (j < lines.length && isBlank(lines[j])) j += 1;
      i = j;
      continue;
    }
    const m = LIST_RE.exec(lines[i]);
    if (!m) break;
    // GFM : après une ligne vide, un item de type opposé (puces ↔ numérotée)
    // au niveau de base démarre une NOUVELLE liste, pas un item de celle-ci.
    if (sawBlank && m[1].length <= baseIndent && /^\d/.test(m[2]) !== baseOrdered) break;
    sawBlank = false;
    const text = m[3];
    const task = TASK_RE.exec(text);
    items.push({
      indent: m[1].length,
      marker: m[2],
      text: task ? task[2] : text,
      task: Boolean(task),
      checked: task ? task[1].trim() !== "" : false,
    });
    i += 1;
  }
  return { items, next: i };
}

/** Construit l'arbre d'imbrication à partir des indentations (pile). */
function buildListTree(items: RawItem[]): ListNode[] {
  const root: ListNode[] = [];
  const stack: Array<{ indent: number; nodes: ListNode[] }> = [{ indent: -1, nodes: root }];
  for (const item of items) {
    let top = stack[stack.length - 1];
    while (stack.length > 1 && item.indent <= top.indent) {
      stack.pop();
      top = stack[stack.length - 1];
    }
    const node: ListNode = { ...item, children: [] };
    top.nodes.push(node);
    stack.push({ indent: item.indent, nodes: node.children });
  }
  return root;
}

function parseBlocks(lines: string[]): Block[] {
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) {
      i += 1;
      continue;
    }

    // Bloc de code fence ``` — tolérant au streaming (fence non fermée).
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE_RE.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // consomme la fence fermante (absente → fin de flux, streaming)
      blocks.push({ type: "code", lang: fence[1], text: body.join("\n") });
      continue;
    }

    const heading = HEADING_RE.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, text: heading[2].trim() });
      i += 1;
      continue;
    }

    if (HR_RE.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }

    // Citation — contenu re-parsé récursivement (citations imbriquées incluses).
    const quote = QUOTE_RE.exec(line);
    if (quote) {
      const inner: string[] = [quote[1]];
      i += 1;
      while (i < lines.length) {
        const next = QUOTE_RE.exec(lines[i]);
        if (next) {
          inner.push(next[1]);
          i += 1;
          continue;
        }
        // Continuation « paresseuse » : texte collé à la citation.
        if (!isBlank(lines[i]) && !isBlockStart(lines[i])) {
          inner.push(lines[i]);
          i += 1;
          continue;
        }
        break;
      }
      blocks.push({ type: "quote", children: parseBlocks(inner) });
      continue;
    }

    // Tableau GFM : en-tête avec « | » + séparateur « --- » contenant « | ».
    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      lines[i + 1].includes("|") &&
      TABLE_SEP_RE.test(lines[i + 1])
    ) {
      const header = splitTableRow(line);
      const align = splitTableRow(lines[i + 1]).map(separatorAlign);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && !isBlank(lines[i]) && lines[i].includes("|")) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      blocks.push({ type: "table", header, align, rows });
      continue;
    }

    const item = LIST_RE.exec(line);
    if (item) {
      const { items, next } = collectListItems(lines, i);
      blocks.push({ type: "list", items: buildListTree(items) });
      i = next;
      continue;
    }

    // Paragraphe : lignes consécutives ; chaque saut simple deviendra <br/>.
    const para: string[] = [line];
    i += 1;
    while (i < lines.length && !isBlank(lines[i]) && !isBlockStart(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: "paragraph", lines: para });
  }
  return blocks;
}

/* =============================== Rendu ================================== */

function cursorNode(key: string): ReactNode {
  return (
    <span key={key} className="chat-message__cursor" aria-hidden="true">
      ▍
    </span>
  );
}

/** Rendu en ligne : code, gras, barré, liens, italique — texte échappé par React. */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let plain = "";
  let serial = 0;
  const flush = () => {
    if (plain !== "") {
      nodes.push(plain);
      plain = "";
    }
  };
  let i = 0;
  while (i < text.length) {
    const rest = text.slice(i);
    const afterWord = wordCharBefore(text, i);
    let m: RegExpExecArray | null;

    m = CODE_SPAN_RE.exec(rest);
    if (m) {
      flush();
      nodes.push(<code key={`${keyPrefix}c${serial++}`}>{m[1]}</code>);
      i += m[0].length;
      continue;
    }

    m = BOLD_RE.exec(rest) ?? (afterWord ? null : BOLD_US_RE.exec(rest));
    if (m) {
      flush();
      nodes.push(
        <strong key={`${keyPrefix}b${serial++}`}>
          {renderInline(m[1], `${keyPrefix}b${serial}-`)}
        </strong>,
      );
      i += m[0].length;
      continue;
    }

    m = STRIKE_RE.exec(rest);
    if (m) {
      flush();
      nodes.push(
        <s key={`${keyPrefix}s${serial++}`}>{renderInline(m[1], `${keyPrefix}s${serial}-`)}</s>,
      );
      i += m[0].length;
      continue;
    }

    m = LINK_RE.exec(rest);
    if (m) {
      flush();
      const url = m[2];
      if (SAFE_URL_RE.test(url)) {
        nodes.push(
          <a key={`${keyPrefix}a${serial++}`} href={url} target="_blank" rel="noreferrer noopener">
            {renderInline(m[1], `${keyPrefix}a${serial}-`)}
          </a>,
        );
      } else {
        // Schème non autorisé : rendu littéral (anti-injection), jamais un <a>.
        nodes.push(m[0]);
      }
      i += m[0].length;
      continue;
    }

    m = EM_RE.exec(rest) ?? (afterWord ? null : EM_US_RE.exec(rest));
    if (m) {
      flush();
      nodes.push(
        <em key={`${keyPrefix}e${serial++}`}>{renderInline(m[1], `${keyPrefix}e${serial}-`)}</em>,
      );
      i += m[0].length;
      continue;
    }

    plain += text.charAt(i);
    i += 1;
  }
  flush();
  return nodes;
}

function renderList(items: ListNode[], key: string): ReactNode {
  const ordered = /^\d/.test(items[0]?.marker ?? "");
  const children = items.map((item, index) => {
    const itemKey = `${key}i${index}-`;
    return (
      <li key={itemKey}>
        {item.task && (
          <input type="checkbox" className="chat-md-task" disabled checked={item.checked} readOnly />
        )}
        {renderInline(item.text, itemKey)}
        {item.children.length > 0 ? renderList(item.children, `${itemKey}s-`) : null}
      </li>
    );
  });
  return ordered ? <ol key={key}>{children}</ol> : <ul key={key}>{children}</ul>;
}

function renderBlock(block: Block, withCursor: boolean, key: string): ReactNode {
  switch (block.type) {
    case "heading": {
      const Tag = `h${block.level}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
      return (
        <Tag key={key}>
          {renderInline(block.text, key)}
          {withCursor ? cursorNode(`${key}cur`) : null}
        </Tag>
      );
    }
    case "paragraph": {
      const children: ReactNode[] = [];
      block.lines.forEach((line, index) => {
        if (index > 0) children.push(<br key={`${key}br${index}`} />);
        children.push(...renderInline(line, `${key}l${index}-`));
      });
      if (withCursor) children.push(cursorNode(`${key}cur`));
      return <p key={key}>{children}</p>;
    }
    case "code":
      return (
        <div key={key} className="chat-md-code" data-lang={block.lang || undefined}>
          <pre>
            <code>{block.text}</code>
          </pre>
        </div>
      );
    case "quote":
      return <blockquote key={key}>{renderBlocks(block.children, false, `${key}q-`)}</blockquote>;
    case "list":
      return renderList(block.items, key);
    case "hr":
      return <hr key={key} />;
    case "table": {
      const alignStyle = (index: number): CSSProperties | undefined => {
        const align = block.align[index];
        return align ? { textAlign: align } : undefined;
      };
      return (
        <div key={key} className="chat-md-table">
          <table>
            <thead>
              <tr>
                {block.header.map((cell, index) => (
                  <th key={`${key}h${index}`} scope="col" style={alignStyle(index)}>
                    {renderInline(cell, `${key}h${index}-`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`${key}r${rowIndex}`}>
                  {row.map((cell, cellIndex) => (
                    <td key={`${key}r${rowIndex}c${cellIndex}`} style={alignStyle(cellIndex)}>
                      {renderInline(cell, `${key}r${rowIndex}c${cellIndex}-`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
  }
  return null; // union exhaustive — apaise le contrôle de flux du compilateur
}

function renderBlocks(blocks: Block[], cursor: boolean, keyPrefix: string): ReactNode[] {
  return blocks.map((block, index) => {
    const key = `${keyPrefix}${index}-`;
    const withCursor = cursor && index === blocks.length - 1;
    const node = renderBlock(block, withCursor, key);
    if (withCursor && block.type !== "paragraph" && block.type !== "heading") {
      // Blocs non textuels : le curseur se place après le bloc.
      return (
        <Fragment key={`${key}wrap`}>
          {node}
          {cursorNode(`${key}cur`)}
        </Fragment>
      );
    }
    return node;
  });
}

/* ============================== Composant =============================== */

interface MarkdownContentProps {
  /** Contenu Markdown brut (peut être incomplet pendant le streaming). */
  content: string;
  /** Affiche le curseur clignotant en fin de dernier bloc (streaming). */
  cursor?: boolean;
}

/**
 * Rend le Markdown d'une bulle assistant. Pure fonction du contenu :
 * `React.memo(ChatMessage)` garantit que le parseur ne tourne pas
 * inutilement sur les messages déjà terminés.
 */
export function MarkdownContent({ content, cursor = false }: MarkdownContentProps): ReactNode {
  const normalized = content.replace(/\r\n?/g, "\n");
  return <>{renderBlocks(parseBlocks(normalized.split("\n")), cursor, "md-")}</>;
}

/* ========================= Variante en ligne ============================ */

interface MarkdownInlineProps {
  /** Texte Markdown en ligne (gras, italique, code, liens). */
  content: string;
}

/**
 * Rend UNIQUEMENT le Markdown en ligne — gras, italique, barré, code, liens —
 * sans aucun bloc englobant (<p>, <ul>, <h*>). Destiné aux contextes compacts
 * (lignes de la trace multi-agents, sous-tâches) où des blocs seraient
 * invalides (<div> dans <span>) ou casseraient la mise en page. Les sauts de
 * ligne deviennent des espaces ; la tolérance streaming s'applique aussi
 * (« ** » non fermé → littéral, liens http(s)/mailto uniquement).
 */
export function MarkdownInline({ content }: MarkdownInlineProps): ReactNode {
  const normalized = content.replace(/\r\n?|\n/g, " ");
  return <>{renderInline(normalized, "mdin-")}</>;
}