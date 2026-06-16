/**
 * CommandPalette.tsx — global ⌘K / Ctrl+K command palette.
 * AC 55, 56, 57, 58, 59.
 *
 * Module-level cache: intentional session-lifetime cache per ADR-10.
 */

import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router';
import type { PaletteIndex } from '../data/types';

// ---------------------------------------------------------------------------
// Module-level session-lifetime cache (ADR-10)
// ---------------------------------------------------------------------------

let paletteCache: PaletteIndex | null = null;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ResultKind = 'ACTION' | 'REPO' | 'TASK';

interface PaletteAction {
  kind: 'ACTION';
  label: string;
  href: string;
}

interface PaletteRepo {
  kind: 'REPO';
  slug: string;
  name: string;
}

interface PaletteTask {
  kind: 'TASK';
  task_id: string;
  title: string;
  repo_slug: string;
  stage: string;
}

type PaletteResult = PaletteAction | PaletteRepo | PaletteTask;

// ---------------------------------------------------------------------------
// Stage chip class (matches TaskDetail)
// ---------------------------------------------------------------------------

function stageChipClass(stage: string): string {
  if (stage === 'DONE') return 'bg-green/20 text-green';
  if (stage.includes('FAIL')) return 'bg-red/20 text-red';
  if (stage === 'CALIBRATED') return 'bg-steel/20 text-steel';
  return 'bg-amber/20 text-amber';
}

// ---------------------------------------------------------------------------
// Result row renderers
// ---------------------------------------------------------------------------

function ActionRow({ item, selected, id }: { item: PaletteAction; selected: boolean; id: string }) {
  return (
    <div
      className={`flex items-center gap-3 px-4 py-2.5 cursor-pointer transition-colors ${
        selected ? 'bg-iron-light border-l-2 border-steel' : 'border-l-2 border-transparent hover:bg-iron-light/50'
      }`}
      role="option"
      aria-selected={selected}
      id={id}
    >
      <span className="font-mono text-[10px] bg-rust/20 text-rust px-1.5 py-0.5 rounded shrink-0">ACTION</span>
      <span className="font-sans text-sm text-ash">{item.label}</span>
    </div>
  );
}

function RepoRow({ item, selected, id }: { item: PaletteRepo; selected: boolean; id: string }) {
  return (
    <div
      className={`flex items-center gap-3 px-4 py-2.5 cursor-pointer transition-colors ${
        selected ? 'bg-iron-light border-l-2 border-steel' : 'border-l-2 border-transparent hover:bg-iron-light/50'
      }`}
      role="option"
      aria-selected={selected}
      id={id}
    >
      <span className="font-mono text-[10px] bg-steel-dark text-steel px-1.5 py-0.5 rounded shrink-0">REPO</span>
      <div className="flex flex-col min-w-0">
        <span className="font-sans text-sm text-ash truncate">{item.name}</span>
        <span className="font-mono text-xs text-sand truncate">{item.slug}</span>
      </div>
    </div>
  );
}

function TaskRow({ item, selected, id }: { item: PaletteTask; selected: boolean; id: string }) {
  return (
    <div
      className={`flex items-center gap-3 px-4 py-2.5 cursor-pointer transition-colors ${
        selected ? 'bg-iron-light border-l-2 border-steel' : 'border-l-2 border-transparent hover:bg-iron-light/50'
      }`}
      role="option"
      aria-selected={selected}
      id={id}
    >
      <span className="font-mono text-[10px] bg-iron-light text-sand px-1.5 py-0.5 rounded shrink-0">TASK</span>
      <div className="flex flex-col min-w-0 flex-1">
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-mono text-xs text-steel shrink-0">{item.task_id}</span>
          <span className="font-sans text-sm text-ash truncate">{item.title}</span>
        </div>
        <div className="flex items-center gap-2 mt-0.5">
          <span className="font-mono text-[10px] text-sand">{item.repo_slug}</span>
          <span className={`font-mono text-[10px] px-1 py-px rounded ${stageChipClass(item.stage)}`}>
            {item.stage}
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton rows — AC 59
// ---------------------------------------------------------------------------

function SkeletonRows() {
  return (
    <div role="status" aria-label="Loading palette results">
      {[...Array(4)].map((_, i) => (
        <div key={i} className="px-4 py-2.5">
          <div className="animate-pulse bg-iron-light h-10 rounded my-1" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main CommandPalette
// ---------------------------------------------------------------------------

export default function CommandPalette() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [paletteIndex, setPaletteIndex] = useState<PaletteIndex | null>(paletteCache ?? null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // ---------------------------------------------------------------------------
  // AC 55 — global ⌘K / Ctrl+K listener
  // ---------------------------------------------------------------------------
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'k' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen(prev => !prev);
        return;
      }
      if (e.key === 'Escape' && open) {
        setOpen(false);
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open]);

  // ---------------------------------------------------------------------------
  // AC 56 — fetch /api/palette-index on first open; 'r' to bust cache
  // ---------------------------------------------------------------------------
  useEffect(() => {
    if (!open) return;

    async function loadIndex() {
      if (paletteCache) return; // already cached
      setLoading(true);
      setFetchError(null);
      try {
        const res = await fetch('/api/palette-index');
        if (!res.ok) {
          setFetchError('Failed to load palette index');
          return;
        }
        paletteCache = (await res.json()) as PaletteIndex;
        setPaletteIndex(paletteCache);
      } catch {
        setFetchError('Network error loading palette index');
      } finally {
        setLoading(false);
      }
    }

    loadIndex();
    // Focus input when opened
    setTimeout(() => inputRef.current?.focus(), 10);
  }, [open]);

  // 'r' to refresh cache when input is empty
  useEffect(() => {
    if (!open) return;
    function handleR(e: KeyboardEvent) {
      if (e.key === 'r' && query === '' && document.activeElement === inputRef.current) {
        e.preventDefault();
        paletteCache = null;
        setLoading(true);
        setFetchError(null);
        fetch('/api/palette-index')
          .then(res => res.ok ? res.json() : Promise.reject(new Error('Failed')))
          .then((data: PaletteIndex) => {
            paletteCache = data;
            setPaletteIndex(data);
          })
          .catch(() => setFetchError('Refresh failed'))
          .finally(() => setLoading(false));
      }
    }
    window.addEventListener('keydown', handleR);
    return () => window.removeEventListener('keydown', handleR);
  }, [open, query]);

  // ---------------------------------------------------------------------------
  // AC 57 — fuzzy search results
  // ---------------------------------------------------------------------------
  const results = useMemo<PaletteResult[]>(() => {
    if (!paletteIndex) return [];
    const q = query.trim().toLowerCase();
    const repos = paletteIndex.repos ?? [];
    const tasks = paletteIndex.tasks ?? [];

    let filteredRepos: typeof repos = [];
    let filteredTasks: typeof tasks = [];

    if (!q) {
      filteredRepos = repos.slice(0, 20);
      filteredTasks = tasks.slice(0, 30);
    } else {
      filteredRepos = repos.filter(r =>
        r.name.toLowerCase().includes(q) || r.slug.toLowerCase().includes(q),
      );
      filteredTasks = tasks.filter(t =>
        t.task_id.toLowerCase().includes(q) || t.title.toLowerCase().includes(q),
      );
    }

    const actions: PaletteAction[] = [];

    // Always show "Go to Home" action when query is empty
    if (!q) {
      actions.push({ kind: 'ACTION', label: 'Go to Home', href: '/' });
    }

    // When exactly 1 repo matches
    if (filteredRepos.length === 1) {
      actions.push({
        kind: 'ACTION',
        label: `Go to repo ${filteredRepos[0].name}`,
        href: `/repo/${filteredRepos[0].slug}`,
      });
    }

    // When exactly 1 task matches
    if (filteredTasks.length === 1) {
      actions.push({
        kind: 'ACTION',
        label: `Jump to task ${filteredTasks[0].task_id}`,
        href: `/repo/${filteredTasks[0].repo_slug}/task/${filteredTasks[0].task_id}`,
      });
    }

    const repoRows: PaletteRepo[] = filteredRepos.map(r => ({
      kind: 'REPO',
      slug: r.slug,
      name: r.name,
    }));

    const taskRows: PaletteTask[] = filteredTasks.map(t => ({
      kind: 'TASK',
      task_id: t.task_id,
      title: t.title,
      repo_slug: t.repo_slug,
      stage: t.stage,
    }));

    const combined: PaletteResult[] = [...actions, ...repoRows, ...taskRows];
    return combined.slice(0, 50);
  }, [query, paletteIndex]);

  // Reset selected index when results change
  useEffect(() => {
    setSelectedIndex(0);
  }, [results]);

  // Scroll selected row into view
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector(`[data-idx="${selectedIndex}"]`) as HTMLElement | null;
    el?.scrollIntoView({ block: 'nearest' });
  }, [selectedIndex]);

  // ---------------------------------------------------------------------------
  // Navigate to a result
  // ---------------------------------------------------------------------------
  const navigateTo = useCallback(
    (result: PaletteResult) => {
      setOpen(false);
      setQuery('');
      if (result.kind === 'ACTION') {
        navigate(result.href);
      } else if (result.kind === 'REPO') {
        navigate(`/repo/${result.slug}`);
      } else {
        navigate(`/repo/${result.repo_slug}/task/${result.task_id}`);
      }
    },
    [navigate],
  );

  // ---------------------------------------------------------------------------
  // AC 58 — keyboard navigation
  // ---------------------------------------------------------------------------
  function handleInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex(prev => (prev + 1) % Math.max(results.length, 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex(prev => (prev - 1 + Math.max(results.length, 1)) % Math.max(results.length, 1));
    } else if (e.key === 'Enter' && results.length > 0) {
      e.preventDefault();
      navigateTo(results[selectedIndex]);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  }

  if (!open) return null;

  return (
    // Fixed overlay — AC 55 modal structure
    <div
      className="fixed inset-0 z-50 bg-graphite/80 backdrop-blur-sm flex items-start justify-center pt-[20vh]"
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      onMouseDown={e => {
        // Close on overlay click (not on modal itself)
        if (e.target === e.currentTarget) setOpen(false);
      }}
    >
      <div className="bg-iron border border-iron-light rounded-xl w-full max-w-xl shadow-2xl p-1 mx-4">
        {/* Search input */}
        <div className="flex items-center gap-2 px-3 py-2 border-b border-iron-light">
          {/* Search icon */}
          <svg aria-hidden width="14" height="14" viewBox="0 0 14 14" fill="none" className="text-sand shrink-0">
            <circle cx="6" cy="6" r="4.5" stroke="currentColor" strokeWidth="1.3" />
            <path d="M9.5 9.5L12 12" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder="Search repos, tasks, or type a command…"
            className="font-mono bg-iron text-ash border-none outline-none w-full text-sm placeholder:text-sand/60"
            aria-label="Command palette search"
            aria-autocomplete="list"
            aria-controls="palette-listbox"
            aria-activedescendant={results.length > 0 ? `palette-option-${selectedIndex}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="font-mono text-[10px] text-sand bg-iron-light px-1.5 py-0.5 rounded border border-iron-light shrink-0">
            ESC
          </kbd>
        </div>

        {/* Results list */}
        <div
          ref={listRef}
          id="palette-listbox"
          role="listbox"
          aria-label="Search results"
          className="max-h-[400px] overflow-y-auto"
        >
          {loading ? (
            <SkeletonRows />
          ) : fetchError ? (
            <div className="px-4 py-6 text-center">
              <p className="font-mono text-xs text-red">{fetchError}</p>
              <p className="font-mono text-xs text-sand mt-1">Press R to retry.</p>
            </div>
          ) : results.length === 0 ? (
            /* AC 59 zero results */
            <div className="px-4 py-8 flex flex-col items-center gap-1">
              <p className="font-mono text-xs text-sand">No results — try a different query.</p>
              <p className="font-mono text-xs text-sand opacity-70">Search by repo slug, task ID, or task title.</p>
            </div>
          ) : (
            results.map((result, idx) => {
              const isSelected = idx === selectedIndex;
              return (
                <div
                  key={
                    result.kind === 'REPO'
                      ? `repo-${result.slug}`
                      : result.kind === 'TASK'
                      ? `task-${result.task_id}`
                      : `action-${result.label}`
                  }
                  data-idx={idx}
                  onMouseEnter={() => setSelectedIndex(idx)}
                  onMouseDown={e => {
                    e.preventDefault();
                    navigateTo(result);
                  }}
                >
                  {result.kind === 'ACTION' && (
                    <ActionRow item={result} selected={isSelected} id={`palette-option-${idx}`} />
                  )}
                  {result.kind === 'REPO' && (
                    <RepoRow item={result} selected={isSelected} id={`palette-option-${idx}`} />
                  )}
                  {result.kind === 'TASK' && (
                    <TaskRow item={result} selected={isSelected} id={`palette-option-${idx}`} />
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Footer hint */}
        <div className="flex items-center gap-4 px-4 py-2 border-t border-iron-light">
          <span className="font-mono text-[10px] text-sand opacity-70">
            <kbd className="bg-iron-light px-1 py-px rounded border border-iron-light">↑↓</kbd> navigate
          </span>
          <span className="font-mono text-[10px] text-sand opacity-70">
            <kbd className="bg-iron-light px-1 py-px rounded border border-iron-light">↵</kbd> select
          </span>
          <span className="font-mono text-[10px] text-sand opacity-70">
            <kbd className="bg-iron-light px-1 py-px rounded border border-iron-light">R</kbd> refresh
          </span>
        </div>
      </div>
    </div>
  );
}
