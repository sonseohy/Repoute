// Repoute 로고 마크: 출발점(원) → 굽은 길 → 화살표. 색은 currentColor 를 따른다.
export function LogoMark({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="3.5" cy="12.5" r="2" />
      <path d="M5.5 12.5h3a2.5 2.5 0 0 0 0-5h-1a2.5 2.5 0 0 1 0-5h5" />
      <path d="M10.5 0.5l2 2-2 2" />
    </svg>
  )
}
