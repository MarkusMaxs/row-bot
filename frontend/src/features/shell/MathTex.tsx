import { useEffect, useState } from 'react';

type Render = (typeof import('katex'))['default']['renderToString'];

let loading: Promise<Render> | null = null;

/**
 * KaTeX and its stylesheet and fonts: bundled with the client (nothing is
 * fetched from anywhere else) and loaded the first time a message has maths.
 */
function loadKatex(): Promise<Render> {
  loading ??= Promise.all([
    import('katex'),
    import('katex/dist/katex.min.css'),
  ]).then(([module]) => module.default.renderToString);
  return loading;
}

/**
 * One formula, typeset. Until KaTeX has loaded, and if the formula can't be
 * read, it shows as written. Display maths scrolls inside its own box.
 */
export default function MathTex({
  tex,
  display,
}: {
  tex: string;
  display: boolean;
}) {
  const [html, setHtml] = useState<{ tex: string; html: string } | null>(null);
  useEffect(() => {
    let alive = true;
    loadKatex().then(
      (render) => {
        if (!alive) return;
        setHtml({
          tex,
          html: render(tex, {
            displayMode: display,
            // A formula KaTeX can't read shows as written, in the error colour.
            throwOnError: false,
            // No \href, \url, \includegraphics or HTML attributes.
            trust: false,
            strict: 'ignore',
            output: 'htmlAndMathml',
            maxExpand: 500,
            maxSize: 20,
          }),
        });
      },
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [tex, display]);
  const className = display ? 'math math-display' : 'math';
  if (html?.tex !== tex)
    return (
      <span className={className} data-pending="true">
        <code>{tex}</code>
      </span>
    );
  return (
    <span
      className={className}
      // KaTeX's own output, with trust off: no links, scripts or styles from the text.
      dangerouslySetInnerHTML={{ __html: html.html }}
    />
  );
}
