import { Link, useParams } from 'react-router'
import type { InquiryMessageView, InquiryView, ReplySummaryView, SendAttemptSummary } from '../../api/types'
import {
  Amount,
  Badge,
  EmptyState,
  ErrorPanel,
  ExternalLink,
  KeyValues,
  LoadingState,
  Notice,
  Section,
  Timestamp,
  ViewMeta,
  Warnings,
} from '../../components/ui'
import { compareInstants, label, shortHash, yesNo } from '../../format'
import { useApiQuery } from '../../hooks/useApiQuery'
import { useMorePages } from '../../hooks/useMorePages'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import {
  InquiryAreaNav,
  InquiryStateBadge,
  LoadMore,
  recipientText,
  ReplyAvailability,
  RequireScope,
  SignalStatusBadge,
  stateHelp,
  suppressionText,
  vehicleText,
  WaitingReasonBadge,
  waitingReasonHelp,
} from './shared'

export function InquiryDetailScreen() {
  return (
    <RequireScope scope="inquiries:read" what="seller inquiries">
      <InquiryDetailContent />
    </RequireScope>
  )
}

function InquiryDetailContent() {
  const { inquiryId = '' } = useParams()
  const { timezone, client } = useWorkspace()
  const query = useApiQuery((api, signal) => api.inquiry(inquiryId, { signal }), [inquiryId])
  const replies = useApiQuery((api, signal) => api.replies({ inquiry_id: inquiryId, limit: 100 }, { signal }), [inquiryId])
  // Further pages of this inquiry's replies (same filter, opaque server cursor): never truncated.
  const moreReplies = useMorePages<ReplySummaryView>(replies.envelope?.request_id ?? null, replies.envelope?.next_cursor ?? null, (cursor) =>
    client.replies({ inquiry_id: inquiryId, limit: 100, cursor }),
  )

  if (query.status === 'loading') return <LoadingState label="Loading the inquiry" />
  if (!query.data || !query.envelope) {
    return (
      <div className="screen">
        <p>
          <Link to="/inquiries">← Seller inquiries</Link>
        </p>
        {query.error ? <ErrorPanel error={query.error} onRetry={query.reload} /> : null}
      </div>
    )
  }
  const inquiry = query.data
  return (
    <div className="screen">
      <p>
        <Link to="/inquiries">← Seller inquiries</Link>
      </p>
      <h1>Inquiry: {vehicleText(inquiry.vehicle)}</h1>
      <InquiryAreaNav />
      <ViewMeta asOf={query.envelope.as_of} fetchedAt={query.fetchedAt} timeZone={timezone} onReload={query.reload} reloading={query.reloading} />
      {query.error ? <ErrorPanel error={query.error} onRetry={query.reload} /> : null}
      <Warnings warnings={query.envelope.warnings} />
      <StatusNotices inquiry={inquiry} />

      <Section title="Status" id="inquiry-status">
        <KeyValues
          items={[
            ['State', <InquiryStateBadge key="s" state={inquiry.state} />],
            ['Meaning', stateHelp(inquiry.state)],
            ['Waiting for', <WaitingReasonBadge key="w" reason={inquiry.waiting_reason} />],
            ['State reasons', inquiry.state_reasons.length ? inquiry.state_reasons.map((code) => <code key={code}>{code} </code>) : 'none'],
            ['Suppression', suppressionText(inquiry.suppression_reason)],
            ['Delivery uncertain', yesNo(inquiry.delivery_uncertain)],
            ['Purpose', label(inquiry.purpose)],
            [
              'Approval',
              <span key="a" data-testid="approval-required">
                none: standing authorization (no per-message approval exists)
              </span>,
            ],
            ['Replies', String(inquiry.reply_count)],
          ]}
        />
      </Section>

      <Section title="Vehicle and listing" id="inquiry-vehicle">
        <KeyValues
          items={[
            ['Listing reference', inquiry.vehicle.listing_reference ?? 'unknown'],
            ['Source', inquiry.vehicle.source_key ?? 'unknown'],
            ['Vehicle identity', inquiry.vehicle.vehicle_kind === 'vehicle_cluster' ? 'confirmed vehicle cluster' : 'one listing'],
            [
              'Listing',
              <span key="l">
                <Link to={`/candidates/${inquiry.vehicle.listing_id}`}>Candidate detail</Link> ·{' '}
                <Link to={`/candidates/${inquiry.vehicle.listing_id}/lifecycle`}>Lifecycle and lags</Link>
              </span>,
            ],
            [
              'Advertisement',
              inquiry.vehicle.listing_url ? (
                <ExternalLink key="u" href={inquiry.vehicle.listing_url}>
                  Open the source listing
                </ExternalLink>
              ) : (
                'not recorded'
              ),
            ],
          ]}
        />
      </Section>

      <Section title="Qualification (bound snapshot)" id="inquiry-qualification">
        <KeyValues
          items={[
            ['Inquiry readiness', <Badge key="r" value={inquiry.qualification.readiness} />],
            [
              'Readiness reasons',
              inquiry.qualification.readiness_reasons.length
                ? inquiry.qualification.readiness_reasons.map((code) => <code key={code}>{code} </code>)
                : 'none',
            ],
            ['Asking price (advertised)', <Amount key="p" value={inquiry.qualification.asking_price} showReason />],
            ['Availability at qualification', label(inquiry.qualification.availability)],
            ['Listing revision', inquiry.qualification.revision_number === null ? 'unknown' : String(inquiry.qualification.revision_number)],
            ['Rules version', inquiry.qualification.rules_version ?? 'unknown'],
            ['Evaluated', <Timestamp key="e" value={inquiry.qualification.evaluated_at} timeZone={timezone} />],
          ]}
        />
        <p className="muted small">Inquiry readiness is separate from investment readiness: it is not a valuation or a purchase decision.</p>
      </Section>

      <Section title="Recipient, language and sender" id="inquiry-recipient">
        <KeyValues
          items={[
            [
              'Recipient verification',
              <Badge key="v" value={inquiry.recipient.verification_status}>
                {recipientText(inquiry.recipient.verification_status)}
              </Badge>,
            ],
            ['Contact kind', label(inquiry.recipient.contact_kind)],
            ['Recipient domain', inquiry.recipient.address_domain ?? 'unknown'],
            [
              'Recipient address',
              inquiry.recipient.address ? (
                <span key="a" className="untrusted" data-testid="recipient-address">
                  {inquiry.recipient.address}
                </span>
              ) : inquiry.recipient.address_redacted ? (
                <span key="r" className="muted" data-testid="recipient-address-withheld">
                  withheld (shown to the owner only)
                </span>
              ) : (
                'none recorded'
              ),
            ],
            ['Verified', <Timestamp key="t" value={inquiry.recipient.verified_at} timeZone={timezone} />],
            ['Contact language', inquiry.recipient.language ?? 'unknown'],
            ['Language status', <Badge key="l" value={inquiry.recipient.language_status} />],
            ['Inquiry language', inquiry.language ?? 'not resolved (never defaults to English)'],
            ['Sender', `${label(inquiry.sender.provider)}${inquiry.sender.display_name ? ` · ${inquiry.sender.display_name}` : ''}`],
            ['Sender binding version', inquiry.sender.binding_version === null ? 'none' : String(inquiry.sender.binding_version)],
            [
              'Standing authorization',
              inquiry.authorization.version === null
                ? 'none recorded'
                : `version ${inquiry.authorization.version} (fingerprint ${shortHash(inquiry.authorization.fingerprint)})`,
            ],
            [
              'Template',
              inquiry.template.template_id
                ? `${inquiry.template.template_id} v${inquiry.template.template_version ?? '?'}`
                : 'not rendered yet',
            ],
          ]}
        />
      </Section>

      <MessageSection message={inquiry.message} />
      <AttemptsSection inquiry={inquiry} timeZone={timezone} />
      <TimelineSection inquiry={inquiry} timeZone={timezone} />

      <Section title="Replies" id="inquiry-replies">
        {replies.status === 'loading' ? <LoadingState label="Loading replies" /> : null}
        {replies.error ? <ErrorPanel error={replies.error} onRetry={replies.reload} /> : null}
        {replies.data ? (
          <>
            <ReplyRows items={[...replies.data.items, ...moreReplies.items]} timeZone={timezone} />
            {moreReplies.error ? <ErrorPanel error={moreReplies.error} onRetry={() => void moreReplies.loadMore()} /> : null}
            {moreReplies.cursor ? (
              <LoadMore cursor={moreReplies.cursor} loading={moreReplies.loading} onLoad={() => void moreReplies.loadMore()} what="replies" />
            ) : null}
          </>
        ) : null}
      </Section>
    </div>
  )
}

function StatusNotices({ inquiry }: { inquiry: InquiryView }) {
  return (
    <>
      {inquiry.delivery_uncertain || inquiry.state === 'uncertain' ? (
        <Notice tone="warn">
          <span data-testid="uncertain-notice">
            A send attempt may have reached the provider. The inquiry is held for reconciliation with positive evidence
            (Sent Items, provider search or a correlated reply) and is never resent blindly.
          </span>
        </Notice>
      ) : null}
      {inquiry.state === 'suppressed' ? (
        <Notice tone="bad">Suppressed ({suppressionText(inquiry.suppression_reason)}): this inquiry will not be sent.</Notice>
      ) : null}
      {inquiry.state === 'held_facts' ? (
        <Notice tone="warn">
          Held: the facts or technical checks listed below are not established yet. This is not an approval request; the
          inquiry proceeds automatically once they are.
        </Notice>
      ) : null}
      {inquiry.waiting_reason !== null && inquiry.waiting_reason !== 'UNCERTAIN_DELIVERY' && inquiry.waiting_reason !== 'NEEDS_FACTS' ? (
        <Notice tone="info">
          <span data-testid="waiting-notice" data-reason={inquiry.waiting_reason}>
            Waiting: {waitingReasonHelp(inquiry.waiting_reason)} It proceeds automatically once that clears; there is no approval
            to give and nothing to send by hand.
          </span>
        </Notice>
      ) : null}
    </>
  )
}

function MessageSection({ message }: { message: InquiryMessageView | null }) {
  return (
    <Section title="Message" id="inquiry-message">
      {message === null ? (
        <EmptyState>No message is rendered yet (it is rendered from the registered template when the inquiry is reserved).</EmptyState>
      ) : (
        <div className="two-columns">
          <div data-testid="original-message">
            <h3>Original (the language sent)</h3>
            <p className="label-like">Subject</p>
            <p className="message-text">{message.original_subject}</p>
            <p className="label-like">Body</p>
            <pre className="message-text">{message.original_body}</pre>
          </div>
          <div data-testid="mk-preview">
            <h3>
              Macedonian preview <Badge tone="info">informational only</Badge>
            </h3>
            <p className="muted small" data-testid="mk-preview-note">
              An informational audit translation of the message above, so you can read what is or was sent. It is not an
              approval draft and does not pause or change the send; the original is authoritative.
            </p>
            {message.mk_preview_body ? (
              <>
                <p className="label-like">Subject</p>
                <p className="message-text" lang="mk">
                  {message.mk_preview_subject ?? ''}
                </p>
                <p className="label-like">Body</p>
                <pre className="message-text" lang="mk">
                  {message.mk_preview_body}
                </pre>
              </>
            ) : (
              <p className="muted">No Macedonian preview was generated.</p>
            )}
          </div>
        </div>
      )}
    </Section>
  )
}

function AttemptsSection({ inquiry, timeZone }: { inquiry: InquiryView; timeZone: string }) {
  const attempts = inquiry.send_attempts
  return (
    <Section title="Send attempts" id="inquiry-attempts">
      <p className="muted small">
        {attempts.count} of at most 3 attempts. &quot;Accepted&quot; means the provider accepted the message; it does not prove
        delivery or reading. An uncertain attempt is reconciled, never resent blindly.
      </p>
      {attempts.attempts.length === 0 ? (
        <EmptyState>No send attempt yet.</EmptyState>
      ) : (
        <table className="responsive-table" aria-label="Send attempts">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Route</th>
              <th scope="col">Outcome</th>
              <th scope="col">Intent committed</th>
              <th scope="col">Finished</th>
              <th scope="col">Reconciliation</th>
              <th scope="col">Error code</th>
            </tr>
          </thead>
          <tbody>
            {attempts.attempts.map((attempt: SendAttemptSummary) => (
              <tr key={attempt.attempt_number}>
                <td data-label="#">{attempt.attempt_number}</td>
                <td data-label="Route">{label(attempt.provider)}</td>
                <td data-label="Outcome">
                  <Badge value={attempt.outcome} tone={attempt.outcome === 'uncertain' ? 'warn' : undefined} />
                  {attempt.submission_uncertain ? <span className="muted small"> submission uncertain</span> : null}
                </td>
                <td data-label="Intent committed">
                  <Timestamp value={attempt.send_intent_committed_at} timeZone={timeZone} />
                </td>
                <td data-label="Finished">
                  <Timestamp value={attempt.finished_at} timeZone={timeZone} />
                </td>
                <td data-label="Reconciliation">
                  {attempt.reconciled_outcome ? (
                    <>
                      {label(attempt.reconciled_outcome)} <Timestamp value={attempt.reconciled_at} timeZone={timeZone} />
                    </>
                  ) : (
                    <span className="muted">not reconciled</span>
                  )}
                </td>
                <td data-label="Error code">{attempt.error_code ? <code>{attempt.error_code}</code> : <span className="muted">none</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

function TimelineSection({ inquiry, timeZone }: { inquiry: InquiryView; timeZone: string }) {
  const t = inquiry.timestamps
  const events: Array<[string, string | null]> = [
    ['Created', t.created_at],
    ['Reserved (quota debited)', t.reserved_at],
    ['Queued', t.queued_at],
    ['Send attempted', t.send_attempted_at],
    ['Accepted by the provider (not delivery)', t.accepted_at],
    ['Seller replied', t.replied_at],
    [`Current state since (${label(inquiry.state)})`, t.state_changed_at],
  ]
  const happened = events.filter((item): item is [string, string] => item[1] !== null).sort((a, b) => compareInstants(a[1], b[1]))
  return (
    <Section title="Timeline" id="inquiry-timeline">
      <ol className="item-list timeline" data-testid="inquiry-timeline">
        {happened.map(([text, at]) => (
          <li key={text}>
            <Timestamp value={at} timeZone={timeZone} /> · {text}
          </li>
        ))}
      </ol>
      <p className="muted small">Last updated <Timestamp value={t.updated_at} timeZone={timeZone} /> (row version {inquiry.row_version}).</p>
    </Section>
  )
}

export function ReplyRows({ items, timeZone }: { items: ReplySummaryView[]; timeZone: string }) {
  if (items.length === 0) return <EmptyState>No replies stored for this inquiry.</EmptyState>
  return (
    <table className="responsive-table" aria-label="Replies">
      <thead>
        <tr>
          <th scope="col">Received</th>
          <th scope="col">Type</th>
          <th scope="col">Language</th>
          <th scope="col">Availability stated</th>
          <th scope="col">Correlation</th>
          <th scope="col">Processing</th>
          <th scope="col">Dot signal</th>
        </tr>
      </thead>
      <tbody>
        {items.map((reply) => (
          <tr key={reply.reply_id} data-testid="reply-row">
            <td data-label="Received">
              <Link className="row-link" to={`/replies/${reply.reply_id}`}>
                <Timestamp value={reply.received_at} timeZone={timeZone} />
              </Link>
            </td>
            <td data-label="Type">
              <Badge value={reply.message_type} tone={reply.message_type === 'seller_reply' ? 'ok' : 'neutral'} />
            </td>
            <td data-label="Language">{reply.original_language ?? 'unknown'}</td>
            <td data-label="Availability stated">
              <ReplyAvailability reply={reply} />
            </td>
            <td data-label="Correlation">
              {reply.quarantined ? <Badge tone="warn">quarantined: unverified match</Badge> : <Badge tone="ok">matched</Badge>}
            </td>
            <td data-label="Processing">
              <Badge value={reply.processing_state} />
            </td>
            <td data-label="Dot signal">
              <SignalStatusBadge status={reply.signal_status} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
