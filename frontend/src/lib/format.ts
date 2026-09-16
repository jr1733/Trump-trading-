/* Formatting helpers.
 *
 * Everything from the API is UTC. Conversion to the viewer's local time happens
 * here and only here, so there is one place to look when a timestamp is wrong.
 */

export function localTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export function localTimeFull(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZoneName: 'short',
  });
}

export function relativeTime(iso: string): string {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

export function signed(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;
}

export function labelColour(label: string): string {
  if (label.includes('BULLISH')) return 'text-bull';
  if (label.includes('BEARISH')) return 'text-bear';
  return 'text-muted';
}

export function returnColour(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'text-muted';
  if (value > 0) return 'text-bull';
  if (value < 0) return 'text-bear';
  return 'text-muted';
}

export function flagLabel(flag: string): { text: string; className: string } {
  switch (flag) {
    case 'ok':
      return { text: 'sample ok', className: 'bg-bull/15 text-bull' };
    case 'limited':
      return { text: 'limited sample', className: 'bg-warn/15 text-warn' };
    default:
      return { text: 'unreliable sample', className: 'bg-bear/15 text-bear' };
  }
}

export function confidenceChip(confidence: string): string {
  switch (confidence) {
    case 'HIGH':
      return 'bg-bull/15 text-bull';
    case 'MEDIUM':
      return 'bg-accent/15 text-accent';
    default:
      return 'bg-edge text-muted';
  }
}

export function titleCase(value: string): string {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}
