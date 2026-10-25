import { Film } from 'lucide-react';
import type { XPostApproval } from '../../api/types';
import { formatBytes } from './MediaPreview';

type XPost = XPostApproval;

const TARGET: Record<XPost['action'], string> = {
  post: '',
  reply: 'Replying to',
  quote: 'Quoting',
  delete: 'Deleting',
};

/**
 * Exactly what a post, reply, quote or delete on X sends, and as which
 * account (N7): the account, the post it answers or quotes as a link, the
 * full text (never cut) and each file by name, size and the start of its
 * SHA-256. The approval is bound to all of it: any change asks again.
 */
export default function XPostDetails({ post }: { post: XPost }) {
  const media = post.media ?? [];
  const text = post.action === 'delete' ? post.target_text : post.text;
  return (
    <div className="x-post-card">
      <p className="x-post-account">
        <span className="x-post-handle">@{post.username}</span>
        {post.name && <span>{post.name}</span>}
        <span className="x-post-id">ID {post.account_id}</span>
      </p>
      {post.target_url && TARGET[post.action] && (
        <p className="x-post-target">
          {TARGET[post.action]}{' '}
          <a href={post.target_url} target="_blank" rel="noopener noreferrer">
            {post.target_url.replace(/^https:\/\//, '')}
          </a>
        </p>
      )}
      {text ? (
        <p className="x-post-text">{text}</p>
      ) : (
        post.action === 'delete' && (
          <p className="x-post-muted">X didn’t say what this post says.</p>
        )
      )}
      {media.length > 0 && (
        <ul
          className="x-post-media"
          aria-label={
            media.length === 1
              ? 'Attached file'
              : `${media.length} attached files`
          }
        >
          {media.map((item) => (
            <li key={`${item.name}-${item.sha256}`}>
              {item.thumbnail ? (
                <img
                  src={item.thumbnail}
                  alt={`Preview of ${item.name}`}
                  width={56}
                  height={56}
                />
              ) : (
                <span className="x-post-media-icon" aria-hidden>
                  <Film size={18} />
                </span>
              )}
              <span className="x-post-media-facts">
                <span className="x-post-media-name">{item.name}</span>
                <span>
                  {formatBytes(item.size)} ·{' '}
                  <abbr title="The start of the file’s SHA-256 fingerprint">
                    SHA-256
                  </abbr>{' '}
                  <code>{item.sha256}</code>
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
