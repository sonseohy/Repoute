// 상단 바(GitHub 헤더 느낌): 로고 · 레포 URL 입력 → POST /repos → owner/name 링크 + 라벨 칩.
import { useState, type FormEvent } from 'react'
import { indexRepo } from '../lib/api'
import type { RepoSummary } from '../types'
import { LogoMark } from './LogoMark'

interface Props {
  repo: RepoSummary | null
  onIndexed: (summary: RepoSummary, url: string) => void
}

export function RepoBar({ repo, onIndexed }: Props) {
  const [url, setUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!url.trim() || loading) return
    load(url.trim())
  }

  async function load(target: string) {
    setLoading(true)
    setError('')
    try {
      onIndexed(await indexRepo(target), target)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <header className="topbar">
      <div className="topbar-row">
        <span className="logo">
          <LogoMark size={20} />
          <span>Repoute</span>
        </span>
        <form className="repo-form" onSubmit={submit}>
          <input
            className="input repo-input"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            aria-label="GitHub 레포 URL"
            spellCheck={false}
          />
          <button className="btn" type="submit" disabled={loading || !url.trim()} aria-label="레포 불러오기">
            {loading ? '인덱싱 중…' : '불러오기'}
          </button>
        </form>
      </div>
      {repo && <RepoSummaryRow repo={repo} />}
      {error && <div className="topbar-row inline-error" role="alert">{error}</div>}
    </header>
  )
}

// repo_id "owner/name@abc1234" → 파란 owner/name 링크 + @sha · N files · cached 라벨 칩
function RepoSummaryRow({ repo }: { repo: RepoSummary }) {
  const [name, sha] = repo.repo_id.split('@')
  return (
    <div className="topbar-row repo-summary" aria-label="레포 요약">
      <a className="repo-name" href={`https://github.com/${name}`} target="_blank" rel="noreferrer">{name}</a>
      {sha && <span className="label-chip">@{sha}</span>}
      <span className="label-chip">{repo.files} files</span>
      <span className="label-chip">{repo.cached ? 'cached' : 'new'}</span>
    </div>
  )
}
