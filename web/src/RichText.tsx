import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import styles from './Workspace.module.css';

export default function RichText({ children, className = '' }: { children: string; className?: string }) {
  return (
    <div className={`${styles.richText} ${className}`.trim()}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children: linkText, href }) =>
            href ? (
              <a href={href} target="_blank" rel="noreferrer">
                {linkText}
              </a>
            ) : (
              <>{linkText}</>
            ),
          img: ({ alt }) => (
            <span className={styles.markdownImagePlaceholder}>[图片：{alt || '未提供说明'}]</span>
          ),
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}
