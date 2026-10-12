import { useT } from '@/components/providers';
import type { SessionReview } from './useSessionReview';

export function SessionReviewBadge({
  review,
  className = '',
}: {
  review: SessionReview | null;
  className?: string;
}) {
  const t = useT();
  if (!review) return null;
  const reason = t(
    review === 'question' ? 'sidebar.waitingForAnswer' : 'sidebar.waitingForApproval',
  );
  return (
    <span
      role="status"
      aria-label={reason}
      title={reason}
      className={`font-medium text-amber-700 dark:text-amber-300 ${className}`}
    >
      {t('sidebar.needsReview')}
    </span>
  );
}
