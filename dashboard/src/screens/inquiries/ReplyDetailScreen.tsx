import { Link, useParams } from 'react-router'
import type { PriceQuoteView, ReplyClaimsView, ReplyView } from '../../api/types'
import { Badge, EmptyState, ErrorPanel, KeyValues, LoadingState, Notice, Section, Timestamp, UntrustedText, ViewMeta, Warnings } from '../../components/ui'
import { bytesText, decimalText, label, shortHash, yesNo } from '../../format'
import { useApiQuery } from '../../hooks/useApiQuery'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import { InquiryAreaNav, requestText, RequireScope, vehicleText } from './shared'

export function ReplyDetailScreen() {
  return (
    <RequireScope scope="inquiries:read" what="seller replies">
      <ReplyDetailContent />
    </RequireScope>
  )
}

function ReplyDetailContent() {
  const { replyId = '' } = useParams()
  const { timezone } = useWorkspace()
  const query = useApiQuery((api, signal) => api.reply(replyId, { signal }), [replyId])
  if (query.status === 'loading') return <LoadingState label="Loading the reply" />
  if (!query.data || !query.envelope) {
    return (
      <div className="screen">
        <p>
          <Link to="/replies">← Seller replies</Link>
        </p>
        {query.error ? <ErrorPanel error={query.error} onRetry={query.reload} /> : null}
      </div>
    )
  }
  const reply = query.data
  return (
    <div className="screen">
      <p>
        <Link to={`/inquiries/${reply.inquiry_id}`}>← Inquiry</Link> · <Link to="/replies">All replies</Link>
      </p>
      <h1>Reply: {vehicleText(reply.vehicle)}</h1>
      <InquiryAreaNav />
      <ViewMeta asOf={query.envelope.as_of} fetchedAt={query.fetchedAt} timeZone={timezone} onReload={query.reload} reloading={query.reloading} />
      {query.error ? <ErrorPanel error={query.error} onRetry={query.reload} /> : null}
      <Warnings warnings={query.envelope.warnings} />
      <p className="muted small">
        Seller statements are evidence for you to verify, never accepted facts. Nothing in this reply is answered, accepted or
        sent automatically.
      </p>
      {reply.quarantined ? (
        <Notice tone="warn">
          <span data-testid="quarantine-notice">
            Quarantined: an unverified possible match ({reply.quarantine_reason ?? 'reason not recorded'}). It does not update the
            vehicle until it is verified.
          </span>
        </Notice>
      ) : null}
      {reply.content_withheld ? (
        <Notice tone="info">
          <span data-testid="content-withheld">
            The text of this reply is withheld until the owner verifies it: a quarantined possible match may be unrelated
            personal mail. Its metadata is shown below.
          </span>
        </Notice>
      ) : null}
      <EscalationPanel claims={reply.claims} unverifiedSender={reply.quarantined || !reply.sender.matches_verified_recipient} />

      <Section title="Message" id="reply-message">
        <KeyValues
          items={[
            ['Type', <Badge key="t" value={reply.message_type} tone={reply.message_type === 'seller_reply' ? 'ok' : 'neutral'} />],
            ['Original language', reply.original_language ?? 'unknown'],
            ['Received', <Timestamp key="r" value={reply.received_at} timeZone={timezone} />],
            ['Observed by the worker', <Timestamp key="o" value={reply.observed_at} timeZone={timezone} />],
            ['Stored by the backend', <Timestamp key="i" value={reply.ingested_at} timeZone={timezone} />],
            ['Processing', <span key="p"><Badge value={reply.processing_state} /> <Timestamp value={reply.processed_at} timeZone={timezone} /></span>],
          ]}
        />
        {reply.content_withheld ? null : (
          <div className="two-columns">
            <div data-testid="reply-original">
              <h3>Original text (untrusted seller data)</h3>
              <p className="label-like">Subject</p>
              <UntrustedText text={reply.subject} as="p" />
              <p className="label-like">Body</p>
              <pre className="message-text untrusted" lang={reply.original_language ?? undefined}>
                {reply.sanitized_body}
              </pre>
            </div>
            <div data-testid="reply-mk-summary">
              <h3>Macedonian summary</h3>
              <p className="muted small">
                A generated summary for reading; amounts, currency and conditions are kept from the original, which stays
                authoritative.
              </p>
              {reply.mk_summary ? (
                <pre className="message-text" lang="mk">
                  {reply.mk_summary}
                </pre>
              ) : (
                <p className="muted">No Macedonian summary yet.</p>
              )}
              {reply.mk_summary_version ? (
                <p className="muted small">
                  Summary {reply.mk_summary_version}, generated <Timestamp value={reply.mk_summary_generated_at} timeZone={timezone} />
                </p>
              ) : null}
            </div>
          </div>
        )}
      </Section>

      {reply.content_withheld ? null : <ClaimsSection claims={reply.claims} />}
      <AttachmentsSection reply={reply} />
      <SenderSection reply={reply} />

      <Section title="Valuation after this reply" id="reply-valuation">
        <KeyValues
          items={[
            ['Valuation state', <Badge key="s" value={reply.valuation.state ?? 'unknown'} />],
            ['Stale reason', reply.valuation.stale_reason ?? 'none'],
            ['Recalculation pending', yesNo(reply.valuation.recalculation_pending)],
            [
              'Valuation',
              reply.valuation.valuation_id ? (
                <Link key="v" to={`/valuations/${reply.valuation.valuation_id}`}>
                  Economics
                </Link>
              ) : (
                'none'
              ),
            ],
          ]}
        />
      </Section>
    </div>
  )
}

/**
 * Requests that need an OWNER decision (payment, reservation, identity documents, ...). When the
 * sender is not verified as the seller (a quarantined possible match: forwarded, changed address or
 * ambiguous; or a sender other than the verified recipient), the requests are attributed to "the
 * sender", never to the seller: anyone who saw the inquiry can write such a message, and a deposit
 * request from an unverified address is the classic payment fraud.
 */
function EscalationPanel({ claims, unverifiedSender }: { claims: ReplyClaimsView | null; unverifiedSender: boolean }) {
  if (!claims || claims.escalations.length === 0) return null
  const who = unverifiedSender ? 'the sender' : 'the seller'
  return (
    <div className="notice notice-bad escalation" role="alert" data-testid="escalations">
      <p className="panel-title">Needs your decision{unverifiedSender ? ': sender not verified as the seller' : ''}</p>
      {unverifiedSender ? (
        <p data-testid="escalation-unverified-sender">
          This message is not verified to come from the seller (it may be forwarded, from a changed address or unrelated). Do
          not pay, reserve or send documents on its basis: confirm the request through the seller&apos;s verified contact first.
          Nothing was accepted, paid, reserved or answered.
        </p>
      ) : (
        <p>The seller&apos;s reply contains requests that only you can decide. Nothing was accepted, paid, reserved or answered.</p>
      )}
      <ul>
        {claims.escalations.map((kind) => (
          <li key={kind} data-testid="escalation">
            <strong>{label(kind)}</strong>: {who} {requestText(kind)}.
          </li>
        ))}
      </ul>
    </div>
  )
}

function quoteAmount(quote: PriceQuoteView): string {
  if (quote.kind === 'range') return `${decimalText(quote.low, quote.currency)} to ${decimalText(quote.high, quote.currency)}`
  if (quote.kind === 'minimum') return `at least ${decimalText(quote.amount ?? quote.low, quote.currency)}`
  return decimalText(quote.amount, quote.currency)
}

function ClaimsSection({ claims }: { claims: ReplyClaimsView | null }) {
  return (
    <Section title="Seller claims (unverified)" id="reply-claims">
      {claims === null ? (
        <EmptyState>No claims were extracted from this reply.</EmptyState>
      ) : (
        <>
          <KeyValues
            items={[
              ['Availability stated', <Badge key="a" value={claims.availability} tone={claims.availability === 'available' ? 'ok' : 'neutral'} />],
              [
                'Unanswered questions',
                claims.unanswered_questions.length ? claims.unanswered_questions.map(label).join(', ') : 'none',
              ],
              ['Other requests', claims.requests.length ? claims.requests.map(label).join(', ') : 'none'],
              ['Claims version', claims.claims_version ?? 'unknown'],
            ]}
          />
          <h3>Quoted prices</h3>
          {claims.price_quotes.length === 0 ? (
            <EmptyState>No price was quoted.</EmptyState>
          ) : (
            <table className="responsive-table" aria-label="Quoted prices">
              <thead>
                <tr>
                  <th scope="col">Quote</th>
                  <th scope="col">Basis and conditions</th>
                  <th scope="col">Status</th>
                  <th scope="col">Excerpt</th>
                </tr>
              </thead>
              <tbody>
                {claims.price_quotes.map((quote, index) => (
                  <tr key={index} data-testid="price-quote">
                    <td data-label="Quote">
                      <span className="amount">{quoteAmount(quote)}</span> <span className="muted small">({label(quote.kind)})</span>
                    </td>
                    <td data-label="Basis and conditions">{quote.conditions.length ? quote.conditions.map(label).join(', ') : 'not stated'}</td>
                    <td data-label="Status">
                      <Badge tone="warn">unaccepted seller quote</Badge>
                      <span className="muted small"> not accepted, not a purchase price; the advertised price is unchanged</span>
                    </td>
                    <td data-label="Excerpt">
                      <UntrustedText text={quote.excerpt} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3>Documents</h3>
          {claims.documents.length === 0 ? (
            <EmptyState>No document statements.</EmptyState>
          ) : (
            <table className="responsive-table" aria-label="Document statements">
              <thead>
                <tr>
                  <th scope="col">Document</th>
                  <th scope="col">Seller says</th>
                  <th scope="col">Excerpt</th>
                </tr>
              </thead>
              <tbody>
                {claims.documents.map((doc, index) => (
                  <tr key={index}>
                    <td data-label="Document">{label(doc.kind)}</td>
                    <td data-label="Seller says">
                      <Badge value={doc.status} tone={doc.status === 'refused' || doc.status === 'not_available' ? 'warn' : 'neutral'} />
                    </td>
                    <td data-label="Excerpt">
                      <UntrustedText text={doc.excerpt} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </Section>
  )
}

function AttachmentsSection({ reply }: { reply: ReplyView }) {
  return (
    <Section title="Attachments (metadata only)" id="reply-attachments">
      <p className="muted small">
        Only safe metadata is stored here: no file bytes, local paths, links or signed access URLs.
        {reply.withheld_sensitive_attachments > 0
          ? ` ${reply.withheld_sensitive_attachments} sensitive attachment(s) were withheld on the owner's PC and never uploaded.`
          : ''}
      </p>
      {reply.attachments.length === 0 ? (
        <EmptyState>{reply.content_withheld ? 'Attachment metadata is withheld with the text.' : 'No attachment metadata.'}</EmptyState>
      ) : (
        <table className="responsive-table" aria-label="Attachments">
          <thead>
            <tr>
              <th scope="col">File name (untrusted)</th>
              <th scope="col">Type</th>
              <th scope="col">Size</th>
              <th scope="col">SHA-256</th>
              <th scope="col">Policy</th>
            </tr>
          </thead>
          <tbody>
            {reply.attachments.map((file) => (
              <tr key={`${file.sha256}-${file.filename}`}>
                <td data-label="File name (untrusted)">
                  <UntrustedText text={file.filename} />
                  {file.document_kind ? <span className="muted small"> ({label(file.document_kind)})</span> : null}
                </td>
                <td data-label="Type">{file.mime_type}</td>
                <td data-label="Size">{bytesText(file.byte_size)}</td>
                <td data-label="SHA-256">
                  <code title={file.sha256}>{shortHash(file.sha256)}</code>
                </td>
                <td data-label="Policy">
                  <Badge value={file.action ?? 'unknown'} tone={file.action === 'allow_vehicle_document' ? 'ok' : 'warn'} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}

function SenderSection({ reply }: { reply: ReplyView }) {
  const sender = reply.sender
  return (
    <Section title="Sender and correlation" id="reply-sender">
      <KeyValues
        items={[
          ['Sender domain', sender.address_domain ?? 'unknown'],
          [
            'Sender address',
            sender.address ? (
              <span key="a" className="untrusted" data-testid="sender-address">
                {sender.address}
              </span>
            ) : (
              <span key="w" className="muted" data-testid="sender-address-withheld">
                withheld (shown to the owner only)
              </span>
            ),
          ],
          ['Matches the verified recipient', yesNo(sender.matches_verified_recipient)],
          ['Correlation', <Badge key="c" value={sender.correlation_status} />],
          ['Linked by message headers', yesNo(sender.header_linked)],
          ['Linked by thread', yesNo(sender.thread_linked)],
          [
            'Correlation reasons',
            sender.correlation_reasons.length ? sender.correlation_reasons.map((code) => <code key={code}>{code} </code>) : 'none',
          ],
        ]}
      />
    </Section>
  )
}
