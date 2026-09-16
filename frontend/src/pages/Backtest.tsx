import { Disclaimer, Section } from '../components/ui';

/* Backtesting is Phase 3. This page exists now so the navigation is stable and
 * so the two things that will govern the feature -- look-ahead prevention and
 * LLM contamination -- are stated before anyone builds expectations on it. */
export default function Backtest() {
  return (
    <div>
      <Section title="Backtesting">
        <div className="card">
          <p className="text-sm text-gray-300">
            Backtesting arrives in Phase 3. It is deliberately separate from live signals, and it
            will ship with two guarantees rather than as a convenience feature.
          </p>
        </div>
      </Section>

      <Section title="Look-ahead prevention">
        <div className="card text-sm text-gray-300">
          <p>
            Similarity, ticker selection, historical statistics and signal weights will use only
            data available before each event's timestamp. Train, validation and test periods are
            separate.
          </p>
          <p className="mt-2 text-xs text-muted">
            The cutoff is already enforced in the engine: every historical query is bounded by the
            subject event's timestamp, and that boundary is covered by tests.
          </p>
        </div>
      </Section>

      <Section title="LLM contamination">
        <div className="card border-warn/40 text-sm text-gray-300">
          <p>
            A language model's training data may already contain knowledge of what happened after a
            historical event. A backtest scored by the model is therefore not a clean test of the
            model.
          </p>
          <p className="mt-2">
            Backtests will offer a <strong>rule-based sentiment mode that uses no LLM</strong>, and
            any backtest run over LLM-scored events will carry a visible{' '}
            <span className="text-warn">potentially contaminated</span> label.
          </p>
        </div>
      </Section>

      <Disclaimer />
    </div>
  );
}
