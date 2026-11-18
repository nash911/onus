/** Project links shown in the header, the landing page and the footer. */
export const REPO_URL = "https://github.com/nash911/onus";
export const CONTRIBUTE_URL = "https://github.com/nash911/onus/issues";
export const AUTHOR_URL = "https://avinashranganath.com";

export function StarIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinejoin="round" aria-hidden="true" focusable="false" className={className}>
      <path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4l-5.9 3.1 1.2-6.5-4.8-4.6 6.6-.9z" />
    </svg>
  );
}
