import type { ReactNode } from 'react'
import type { SettingsView } from '../api/types'
import { Badge, EmptyState, ErrorPanel, KeyValues, LoadingState, Notice, Section, Timestamp, ViewMeta, Warnings } from '../components/ui'
import { groupDecimal, label, yesNo } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useWorkspace } from '../workspace/WorkspaceProvider'

export function SettingsScreen() {
  const { timezone } = useWorkspace()
  const settings = useApiQuery((client, signal) => client.settings({ signal }), [])
  return (
    <div className="screen">
      <h1>Settings</h1>
      <p className="muted">Read-only. Configuration changes are owner-only operations outside this dashboard.</p>
      {settings.status === 'loading' ? <LoadingState label="Loading settings" /> : null}
      {settings.error ? <ErrorPanel error={settings.error} onRetry={settings.reload} /> : null}
      {settings.data && settings.envelope ? (
        <>
          <ViewMeta asOf={settings.envelope.as_of} fetchedAt={settings.fetchedAt} timeZone={timezone} onReload={settings.reload} reloading={settings.reloading} />
          <Warnings warnings={settings.envelope.warnings} />
          <SettingsBody settings={settings.data} timeZone={timezone} />
        </>
      ) : null}
    </div>
  )
}

function SettingsBody({ settings, timeZone }: { settings: SettingsView; timeZone: string }) {
  const threshold = settings.contribution_threshold
  return (
    <>
      <Section title="Profile revisions" id="profiles">
        {settings.config_revision ? (
          <p>
            Configuration revision {settings.config_revision.revision} from{' '}
            <Timestamp value={settings.config_revision.created_at} timeZone={timeZone} />
            {settings.config_revision.reason ? <span className="muted"> ({settings.config_revision.reason})</span> : null}
          </p>
        ) : (
          <Notice tone="warn">No configuration revision is recorded; the file defaults are shown.</Notice>
        )}
        <table className="responsive-table" aria-label="Search profiles">
          <thead>
            <tr>
              <th scope="col">Profile</th>
              <th scope="col">Status</th>
              <th scope="col">Price band (EUR)</th>
              <th scope="col">Mileage below (km)</th>
              <th scope="col">Countries</th>
            </tr>
          </thead>
          <tbody>
            {settings.profiles.map((profile) => (
              <tr key={profile.profile_key} className={profile.enabled ? undefined : 'row-disabled'} data-testid={`profile-${profile.profile_key}`}>
                <td data-label="Profile">
                  {profile.label}
                  <div className="muted small">{profile.queue_label}</div>
                </td>
                <td data-label="Status">
                  <Badge tone={profile.enabled ? 'ok' : 'muted'}>{profile.enabled ? 'ENABLED' : 'DISABLED'}</Badge>
                  <div className="small">{profile.status_label}</div>
                </td>
                <td data-label="Price band (EUR)">
                  {profile.min_price_eur ? groupDecimal(profile.min_price_eur) : 'no minimum'} – {groupDecimal(profile.max_price_eur)}
                  {profile.max_price_inclusive ? ' (inclusive)' : ' (exclusive)'}
                </td>
                <td data-label="Mileage below (km)">{groupDecimal(profile.max_mileage_km_exclusive)}</td>
                <td data-label="Countries">{profile.source_countries.join(', ') || 'none'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Contribution threshold" id="threshold">
        <p data-testid="settings-threshold">
          EUR {groupDecimal(threshold.amount_eur)}{' '}
          <Badge tone={threshold.label === 'PROPOSED' ? 'warn' : 'ok'}>{threshold.label}</Badge>{' '}
          {threshold.approval_status === 'unapproved' ? <span className="muted">(not approved by the owner)</span> : null}
        </p>
        <p className="muted small">{threshold.note}</p>
        <p>
          MK resale band EUR {groupDecimal(settings.mk_resale_band.min_eur)} – {groupDecimal(settings.mk_resale_band.max_eur)}
          <span className="muted"> ({settings.mk_resale_band.meaning})</span>
        </p>
        <p>
          Price re-alert policy: EUR {groupDecimal(settings.price_realert_policy.abs_eur)} or {settings.price_realert_policy.pct}%{' '}
          <Badge tone={settings.price_realert_policy.label === 'PROPOSED' ? 'warn' : 'ok'}>{settings.price_realert_policy.label}</Badge>
        </p>
      </Section>

      <Section title="Destination bindings" id="bindings">
        {settings.destination_bindings.length === 0 ? (
          <EmptyState>No destination bindings.</EmptyState>
        ) : (
          <table className="responsive-table">
            <thead>
              <tr>
                <th scope="col">Binding</th>
                <th scope="col">Enabled</th>
                <th scope="col">Approval</th>
                <th scope="col">Verified</th>
                <th scope="col">External ids</th>
              </tr>
            </thead>
            <tbody>
              {settings.destination_bindings.map((binding) => (
                <tr key={binding.binding_id}>
                  <td data-label="Binding">
                    {binding.label}
                    <div className="muted small">{label(binding.provider)}</div>
                  </td>
                  <td data-label="Enabled">{yesNo(binding.enabled)}</td>
                  <td data-label="Approval">{binding.approval_recorded ? 'recorded' : 'not recorded'}</td>
                  <td data-label="Verified">
                    <Timestamp value={binding.verified_at} timeZone={timeZone} />
                  </td>
                  <td data-label="External ids">
                    {[binding.external_workspace_id, binding.external_channel_id].filter(Boolean).join(' / ') || 'none'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Activation gates" id="gates">
        <table className="responsive-table">
          <thead>
            <tr>
              <th scope="col">Capability</th>
              <th scope="col">Status</th>
              <th scope="col">Dependency</th>
              <th scope="col">Required evidence</th>
            </tr>
          </thead>
          <tbody>
            {settings.gates.map((gate) => (
              <tr key={gate.capability}>
                <td data-label="Capability">
                  <code>{gate.capability}</code>
                </td>
                <td data-label="Status">
                  <Badge value={gate.status} tone={gate.status === 'active' || gate.status === 'live_verified' ? 'ok' : gate.status === 'blocked' ? 'bad' : 'neutral'} />
                </td>
                <td data-label="Dependency">{gate.dependency}</td>
                <td data-label="Required evidence">{gate.required_evidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Administration" id="administration">
        <p className="muted">{settings.administration_note}</p>
        {settings.can_administer ? (
          <div data-testid="owner-admin">
            <p>
              You are an owner. Owner-only details for audits (changes still go through the guarded owner tooling, not this
              page):
            </p>
            <KeyValues
              items={[
                ['Configuration revision id', settings.config_revision ? <code key="c">{settings.config_revision.config_revision_id}</code> : 'none'],
                ...settings.profiles.map(
                  (profile): [string, ReactNode] => [
                    `Profile ${profile.profile_key} row`,
                    profile.row_version === null ? 'file default' : `version ${profile.row_version}`,
                  ],
                ),
                ...settings.destination_bindings.map(
                  (binding): [string, ReactNode] => [
                    `Binding ${binding.label}`,
                    <span key={binding.binding_id}>
                      <code>{binding.binding_id}</code> · version {binding.row_version}
                    </span>,
                  ],
                ),
                ...settings.gates
                  .filter((gate) => gate.owner || gate.next_action)
                  .map((gate): [string, ReactNode] => [`Gate ${gate.capability}`, `${gate.owner ?? 'no owner'} · next: ${gate.next_action ?? 'none'}`]),
              ]}
            />
          </div>
        ) : null}
      </Section>
    </>
  )
}
