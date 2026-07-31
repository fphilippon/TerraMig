const stages = ['created', 'discovered', 'matched', 'generated', 'validated', 'submitted', 'imported'];
const screenContent = [
  ['SCOPE', 'Choose adoption scope', 'Confirm the GCP source and HCP Terraform destination before discovery starts.'],
  ['DISCOVERY', 'Review existing infrastructure', 'Inspect resources, attributes, and dependencies discovered in the selected project.'],
  ['MODULE MATCHING', 'Select the Terraform representation', 'Choose an HCP private module, a verified public module, or the direct resource fallback.'],
  ['AI COMPOSITION', 'Review generated Terraform', 'Inspect the agent transcript, module decisions, code, and import mappings.'],
  ['LOCAL VERIFICATION', 'Review validation and planned drift', 'Terraform validation and complete import coverage must pass. Verified drift requires explicit operator acceptance.'],
  ['GIT DELIVERY', 'Deliver governed source', 'Use a model repository, overlay the verified bundle, and push a migration branch or pull request.'],
  ['FINAL APPROVAL', 'Import into HCP Terraform', 'Track saved-plan submission, manual HCP apply, and the post-import zero-drift plan.'],
];
const pageContent = {
  workflow: ['INFRASTRUCTURE ADOPTION', '', 'Discover what exists, resolve how it connects, and let an AI agent compose selected private or public modules—or verified provider resources—into safe Terraform imports.'],
  inventory: ['GCP INVENTORY', 'See what already exists.', 'Inspect the normalized Cloud Asset Inventory before asking the agent to make any composition decision.'],
  modules: ['PRIVATE LIBRARY GOVERNANCE', 'Inspect approved building blocks.', 'Review verified HCP Terraform library-module ownership schemas, integrity, and GCP coverage used by the matching agent.'],
  operations: ['OPERATIONS', 'Observe every migration.', 'Track workflow stages, failures, request telemetry, and downloadable evidence from one operational view.'],
  settings: ['CONFIGURATION', 'Connect TerraMig safely.', 'Configure production integrations, credentials, the HCP Terraform target, and workflow readiness.'],
};

let workflow = null;
let settings = null;
let inventoryResources = [];
let currentScreen = 0;
let wizardBusy = false;
let resourceVisibility = 'supported';
let resourceManagementVisibility = 'unmanaged';
let resourceGrouping = 'domain';
let resourceSearch = '';
let matchGrouping = 'decision';
let matchSearch = '';
const expandedResourceGroups = new Set();
const expandedMatchGroups = new Set();
let currentUser = null;
let csrfToken = '';
let applicationStarted = false;
let activeJob = null;
let phaseJobLogs = {};
let gitDeliveryFormWorkflowId = null;
let driftAcceptanceRenderKey = '';
const lifecycleMonitors = new Set();
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch(path, {...options, method, headers, credentials: 'same-origin'});
  const data = await response.json();
  if (response.status === 401) showLogin();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function boot() {
  bindEvents();
  await checkSession();
}

async function checkSession() {
  try {
    const response = await fetch('/api/auth/session', {credentials: 'same-origin'});
    if (!response.ok) {
      showLogin();
      return;
    }
    const session = await response.json();
    currentUser = session.user;
    csrfToken = readCookie('terramig_csrf');
    if (currentUser.must_change_password) {
      showPasswordChange();
      return;
    }
    await startApplication(session.persistence);
  } catch (error) {
    showLogin(error.message);
  }
}

async function startApplication(persistence = null) {
  $('auth-gate').classList.add('hidden');
  $('app-shell').classList.remove('hidden');
  $('current-user').textContent = currentUser?.username || 'local user';
  if (!applicationStarted) {
    renderWizard();
    applicationStarted = true;
  }
  settings = await api('/api/settings');
  renderSettings();
  const storage = persistence || settings.persistence;
  $('persistence-mode').textContent = storage?.durable ? 'PostgreSQL · durable' : 'Memory · development';
  await loadProjects();
}

function bindEvents() {
  $('login-form').addEventListener('submit', login);
  $('password-change-form').addEventListener('submit', changePassword);
  $('logout').addEventListener('click', logout);
  document.querySelectorAll('.nav-link').forEach(link => link.addEventListener('click', () => showView(link.dataset.view)));
  document.querySelectorAll('.adoption-step').forEach(step => step.addEventListener('click', () => openWizardScreen(Number(step.dataset.screen))));
  $('wizard-previous').addEventListener('click', () => openWizardScreen(currentScreen - 1));
  $('wizard-next').addEventListener('click', goNext);
  $('adoption-confirm').addEventListener('change', configureNextButton);
  $('allow-nonempty-workspace').addEventListener('change', configureNextButton);
  $('accept-drift').addEventListener('change', configureNextButton);
  $('git-create-pr').addEventListener('change', configureNextButton);
  $('copy').addEventListener('click', copyTerraform);
  $('load-inventory').addEventListener('click', loadInventory);
  $('load-modules').addEventListener('click', () => loadModules(true));
  $('operations-refresh').addEventListener('click', loadOperations);
  $('jobs-body').addEventListener('click', controlJob);
  $('persisted-workflows').addEventListener('click', resumeWorkflow);
  $('download-workflow-report').addEventListener('click', downloadReport);
  $('new-workflow').addEventListener('click', resetWorkflow);
  $('settings-form').addEventListener('submit', saveSettings);
  $('ai-provider').addEventListener('change', renderAiCredential);
  $('ai-credential-save').addEventListener('click', saveAiCredential);
  $('ai-credential-test').addEventListener('click', testAiCredential);
  $('ai-credential-clear').addEventListener('click', clearAiCredential);
  $('hcp-credential-save').addEventListener('click', saveHcpCredential);
  $('hcp-credential-test').addEventListener('click', testHcpCredential);
  $('hcp-credential-clear').addEventListener('click', clearHcpCredential);
  $('github-credential-save').addEventListener('click', saveGithubCredential);
  $('github-credential-test').addEventListener('click', testGithubCredential);
  $('github-credential-clear').addEventListener('click', clearGithubCredential);
  $('gcp-credential-save').addEventListener('click', saveAndTestGcpConnection);
  $('gcp-credential-clear').addEventListener('click', clearGcpCredential);
  $('gcp-connection-reset').addEventListener('click', resetGcpConnection);
  $('gcp-connection-test').addEventListener('click', testGcpConnection);
  $('gcp-credential-file').addEventListener('change', loadGcpCredentialFile);
  $('gcp-project-id').addEventListener('input', renderGcpConnection);
  $('gcp-credential').addEventListener('input', renderGcpConnection);
  $('git-ssh-credential-save').addEventListener('click', saveGitSshCredential);
  $('git-ssh-credential-test').addEventListener('click', testGitSshCredential);
  $('git-ssh-credential-clear').addEventListener('click', clearGitSshCredential);
  ['resources', 'match-list'].forEach(id => $(id).addEventListener('click', openWorkflowResource));
  $('resources').addEventListener('click', handleResourceGroupAction);
  $('match-list').addEventListener('click', handleMatchGroupAction);
  $('resources').addEventListener('change', updateResourceSelection);
  $('resource-visibility').addEventListener('change', event => {
    resourceVisibility = event.target.value === 'all' ? 'all' : 'supported';
    renderWorkflowResources();
  });
  $('resource-management').addEventListener('change', event => {
    resourceManagementVisibility = ['tracked', 'all'].includes(event.target.value) ? event.target.value : 'unmanaged';
    renderWorkflowResources();
  });
  $('resource-grouping').addEventListener('change', event => {
    resourceGrouping = ['service', 'location', 'support'].includes(event.target.value) ? event.target.value : 'domain';
    expandedResourceGroups.clear();
    renderWorkflowResources();
  });
  $('resource-search').addEventListener('input', event => {
    resourceSearch = event.target.value.trim().toLowerCase();
    renderWorkflowResources();
  });
  $('match-grouping').addEventListener('change', event => {
    matchGrouping = ['domain', 'service', 'location'].includes(event.target.value) ? event.target.value : 'decision';
    expandedMatchGroups.clear();
    renderModuleMatches();
  });
  $('match-search').addEventListener('input', event => {
    matchSearch = event.target.value.trim().toLowerCase();
    renderModuleMatches();
  });
  $('match-list').addEventListener('change', updateModuleSelection);
  $('select-supported-resources').addEventListener('click', selectSupportedResources);
  $('clear-resource-selection').addEventListener('click', clearResourceSelection);
  $('select-latest-compatible-modules').addEventListener('click', () => applyModuleSelectionPolicy('latest-compatible'));
  $('select-direct-resources').addEventListener('click', () => applyModuleSelectionPolicy('direct'));
  $('search-public-modules').addEventListener('click', searchPublicModules);
  $('return-to-match').addEventListener('click', returnToModuleMatching);
  $('inventory-body').addEventListener('click', event => {
    const row = event.target.closest('[data-inventory-index]');
    if (row) openResourceDrawer(inventoryResources[Number(row.dataset.inventoryIndex)], []);
  });
  $('drawer-close').addEventListener('click', closeResourceDrawer);
  $('drawer-backdrop').addEventListener('click', closeResourceDrawer);
}

async function login(event) {
  event.preventDefault();
  setBusy($('login-submit'), true);
  $('auth-error').classList.add('hidden');
  try {
    const result = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        username: $('login-username').value,
        password: $('login-password').value,
      }),
    });
    currentUser = result.user;
    csrfToken = result.csrf_token;
    if (currentUser.must_change_password) {
      $('current-password').value = $('login-password').value;
      showPasswordChange();
    } else {
      await startApplication();
    }
  } catch (error) {
    $('auth-error').textContent = error.message;
    $('auth-error').classList.remove('hidden');
  } finally {
    setBusy($('login-submit'), false);
  }
}

async function changePassword(event) {
  event.preventDefault();
  $('password-error').classList.add('hidden');
  if ($('new-password').value !== $('confirm-password').value) {
    $('password-error').textContent = 'New password confirmation does not match.';
    $('password-error').classList.remove('hidden');
    return;
  }
  setBusy($('password-submit'), true);
  try {
    const result = await api('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({
        current_password: $('current-password').value,
        new_password: $('new-password').value,
      }),
    });
    currentUser = result.user;
    csrfToken = result.csrf_token;
    $('login-password').value = '';
    $('current-password').value = '';
    $('new-password').value = '';
    $('confirm-password').value = '';
    await startApplication();
  } catch (error) {
    $('password-error').textContent = error.message;
    $('password-error').classList.remove('hidden');
  } finally {
    setBusy($('password-submit'), false);
  }
}

async function logout() {
  try {
    await api('/api/auth/logout', {method: 'POST', body: '{}'});
  } catch (_) {
    // Local UI state is cleared even if the session already expired.
  }
  currentUser = null;
  csrfToken = '';
  settings = null;
  workflow = null;
  applicationStarted = false;
  $('app-shell').classList.add('hidden');
  showLogin();
}

function showLogin(message = '') {
  $('auth-gate').classList.remove('hidden');
  $('app-shell').classList.add('hidden');
  $('login-form').classList.remove('hidden');
  $('password-change-form').classList.add('hidden');
  $('auth-error').classList.toggle('hidden', !message);
  $('auth-error').textContent = message;
}

function showPasswordChange() {
  $('auth-gate').classList.remove('hidden');
  $('app-shell').classList.add('hidden');
  $('login-form').classList.add('hidden');
  $('password-change-form').classList.remove('hidden');
}

function readCookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split('; ').find(value => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : '';
}

function showView(name) {
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('hidden', view.id !== `view-${name}`));
  document.querySelectorAll('nav .nav-link').forEach(link => link.classList.toggle('active', link.dataset.view === name));
  [$('page-kicker').textContent, $('page-title').textContent, $('page-lede').textContent] = pageContent[name];
  $('page-title').classList.toggle('hidden', !$('page-title').textContent);
  clearMessages();
  if (name === 'workflow') renderWizard();
  if (name === 'modules') loadModules();
  if (name === 'operations') loadOperations();
}

function openWizardScreen(index) {
  if (wizardBusy || index < 0 || index > maxAccessibleScreen()) return;
  currentScreen = index;
  clearMessages();
  renderWizard();
}

function maxAccessibleScreen() {
  if (!workflow || workflow.stage === 'created') return 0;
  if (workflow.stage === 'discovered') return 1;
  if (workflow.stage === 'matched') return workflow.events?.some(event => event.startsWith('AI composition queued')) ? 3 : 2;
  if (workflow.stage === 'generated') return workflow.verification ? 4 : 3;
  if (workflow.stage === 'validated') {
    return ['branch-pushed', 'pull-request-open'].includes(workflow.git_delivery?.status) ? 6 : 5;
  }
  return 6;
}

async function goNext() {
  if (wizardBusy) return;
  // Capture editable Git inputs before the busy-state render. A failed delivery is
  // retained as result data, but must never replace the values chosen for a retry.
  const gitDeliveryPayload = currentScreen === 5 ? readGitDeliveryForm() : null;
  wizardBusy = true;
  clearMessages();
  renderWizard();
  try {
    if (currentScreen === 0) {
      if (!workflow) workflow = await api('/api/workflows', {method: 'POST', body: JSON.stringify({project_id: $('project').value})});
      if (workflow.stage === 'created') workflow = await runAction('discover');
      currentScreen = 1;
    } else if (currentScreen === 1) {
      if (!workflow.selected_resource_ids?.length) throw new Error('Select at least one resource to adopt.');
      if (['discovered', 'matched'].includes(workflow.stage)) workflow = await runAction('match', {selected_resource_ids: workflow.selected_resource_ids, include_public: Boolean(workflow.public_registry_searched)});
      currentScreen = 2;
    } else if (currentScreen === 2) {
      currentScreen = 3;
      renderWizard();
      if (workflow.stage === 'matched') workflow = await runAction('generate', {module_selections: workflow.module_selections || {}});
    } else if (currentScreen === 3) {
      if (workflow.stage === 'matched') {
        workflow = await runAction('generate', {module_selections: workflow.module_selections || {}});
      } else if (workflow.stage === 'generated') {
        workflow = await runAction('validate');
        currentScreen = 4;
      }
    } else if (currentScreen === 4) {
      if (workflow.stage === 'generated' && driftAcceptanceEligible()) {
        if (!$('accept-drift').checked) throw new Error('Explicitly accept the detected drift before continuing.');
        workflow = await runAction('accept-drift', {accept: true});
      } else if (workflow.stage === 'generated') {
        workflow = await runAction('validate');
      }
      if (['validated', 'submitted', 'imported'].includes(workflow.stage)) currentScreen = 5;
    } else if (currentScreen === 5 && workflow.stage === 'validated') {
      if (!['branch-pushed', 'pull-request-open'].includes(workflow.git_delivery?.status)) {
        if (!gitDeliveryPayload.model_repository) throw new Error('Provide the model repository.');
        if (!gitDeliveryPayload.target_repository) throw new Error('Provide the target application repository.');
        workflow = await runAction('deliver', gitDeliveryPayload);
        showSuccess(workflow.git_delivery.pull_request_url ? 'Git pull request opened for review.' : 'Git delivery branch pushed.');
      }
      currentScreen = 6;
    } else if (currentScreen === 6 && workflow.stage === 'validated') {
      if (!$('adoption-confirm').checked) throw new Error('Authorize the HCP saved-plan submission first.');
      workflow = await runAction('import', {
        confirm: true,
        allow_nonempty_workspace: $('allow-nonempty-workspace').checked,
      });
      showSuccess(`HCP saved plan ${workflow.import_run_id} submitted for remote destruction checks. Do not apply it until the “Remote destroy gate” shows Passed.`);
      monitorHcpImport(workflow.id);
    }
  } catch (error) { showError(error); }
  finally {
    wizardBusy = false;
    currentScreen = Math.min(currentScreen, maxAccessibleScreen());
    renderWizard();
  }
}

async function runAction(action, payload = {}) {
  activeJob = await api(`/api/workflows/${workflow.id}/${action}`, {method: 'POST', body: JSON.stringify(payload)});
  renderWizard();
  await waitForJob(activeJob);
  const latest = await api(`/api/workflows/${workflow.id}`);
  activeJob = null;
  return latest;
}

async function loadProjects() {
  try {
    const data = await api('/api/projects');
    const options = data.projects.map(project => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)} · ${escapeHtml(project.id)}</option>`).join('');
    $('project').innerHTML = options || '<option>No projects found</option>';
    $('inventory-project').innerHTML = options || '<option>No projects found</option>';
  } catch (error) {
    const message = settings ? 'Complete production integration readiness to load projects' : error.message;
    $('project').innerHTML = `<option>${escapeHtml(message)}</option>`;
    $('inventory-project').innerHTML = `<option>${escapeHtml(message)}</option>`;
  }
}

async function loadInventory() {
  setBusy($('load-inventory'), true);
  try {
    const data = await api(`/api/inventory?project_id=${encodeURIComponent($('inventory-project').value)}`);
    inventoryResources = data.resources;
    $('inventory-count').textContent = `${data.resources.length} resources`;
    const coverage = data.service_coverage;
    $('inventory-services').innerHTML = coverage.services.length
      ? coverage.services.map(service => `<div class="service-chip"><strong>${escapeHtml(service.service)}</strong><span>${service.resources} resource${service.resources === 1 ? '' : 's'}</span><span>${service.asset_types.length} asset type${service.asset_types.length === 1 ? '' : 's'}</span><span>${service.by_status.supported || 0} supported</span></div>`).join('')
      : '<span class="muted-copy">No GCP services were discovered in this project.</span>';
    $('inventory-body').innerHTML = data.resources.length ? data.resources.map((resource, index) => `<tr class="clickable-row" data-inventory-index="${index}"><td><strong>${escapeHtml(resource.name)}</strong><small>${escapeHtml(resource.id)}</small></td><td><code>${escapeHtml(resource.type)}</code></td><td>${supportBadge(resource)}</td><td>${escapeHtml(resource.location)}</td><td>${resource.dependency_ids.length}<span class="row-action">View details →</span></td></tr>`).join('') : '<tr><td colspan="5" class="empty-cell">No resources found.</td></tr>';
  } catch (error) { showError(error); }
  finally { setBusy($('load-inventory'), false); }
}

async function loadModules(force = false) {
  setBusy($('load-modules'), true);
  try {
    const current = await api('/api/catalog/status');
    if (force || current.status !== 'verified') {
      const scan = await api('/api/catalog/scan', {
        method: 'POST',
        body: JSON.stringify({force}),
      });
      await waitForCatalogJob(scan);
    }
    const data = await api('/api/modules');
    const [catalog, coverage] = await Promise.all([
      api('/api/catalog/status'),
      api('/api/capabilities'),
    ]);
    $('module-count').textContent = `${data.modules.length} modules`;
    $('catalog-status').textContent = `${catalog.status}${catalog.signed ? ' · signed' : ''}${catalog.trusted_modules ? ` · ${catalog.trusted_modules} trusted` : ''}${catalog.incompatible_modules ? ` · ${catalog.incompatible_modules} incompatible` : ''}${catalog.needs_review_modules ? ` · ${catalog.needs_review_modules} review` : ''}${catalog.failures ? ` · ${catalog.failures} failed` : ''}`;
    $('catalog-digest').textContent = catalog.sha256 ? catalog.sha256.slice(0, 20) : catalog.message || '—';
    const baseDomains = coverage.domains || {};
    const baseSummary = [
      ['Compute', baseDomains.compute],
      ['Storage', baseDomains.storage],
      ['Databases', baseDomains.databases],
    ].filter(([, domain]) => domain).map(([label, domain]) => `${label} ${domain.supported}/${domain.total}`).join(' · ');
    $('capability-coverage').textContent = `${coverage.supported} supported · ${coverage.partial} partial · ${coverage.total_asset_types} total${baseSummary ? ` · ${baseSummary}` : ''}`;
    $('module-grid').innerHTML = data.modules.length ? data.modules.map(module => `<article class="module-card"><div class="module-icon">TF</div><div><strong>${escapeHtml(module.source)}</strong><p>${escapeHtml(module.description || 'HCP private library module')}</p><div class="module-meta"><span>v${escapeHtml(module.version)}</span><span>${module.registry_kind === 'hcp-public' ? 'HCP-approved public' : 'HCP private'}</span><span>${module.inputs.length} inputs</span><span>${module.outputs.length} outputs</span><span>${escapeHtml(module.schema_status || 'unknown')} schema</span></div></div></article>`).join('') : '<div class="empty-cell">No Google modules found in the HCP private library.</div>';
  } catch (error) { $('module-grid').innerHTML = `<div class="empty-cell">${escapeHtml(error.message)}</div>`; }
  finally { setBusy($('load-modules'), false); }
}

async function waitForCatalogJob(initialJob) {
  let job = initialJob;
  renderCatalogJob(job);
  while (['queued', 'running'].includes(job.status)) {
    await new Promise(resolve => setTimeout(resolve, 750));
    job = await api(`/api/jobs/${job.id}`);
    renderCatalogJob(job);
  }
  if (job.status === 'failed') throw new Error(job.last_error || 'Private-module scan failed');
  if (job.status === 'cancelled') throw new Error('Private-module scan was cancelled');
  return job;
}

function renderCatalogJob(job) {
  const running = ['queued', 'running'].includes(job.status);
  $('catalog-scan-status').textContent = running ? job.status : job.status === 'succeeded' ? 'Complete' : job.status;
  $('catalog-scan-status').className = `agent-run-status ${running ? 'running' : job.status === 'succeeded' ? 'accepted' : job.status}`;
  $('catalog-scan-label').textContent = `${job.kind}.${job.action} · attempt ${job.attempts || 0}/${job.max_attempts}`;
  const logs = job.result?.logs || [];
  $('catalog-scan-log').textContent = logs.length ? logs.map(line => `$ ${line}`).join('\n') : '$ Waiting for the durable catalog worker…';
  $('catalog-scan-log').parentElement.scrollTop = $('catalog-scan-log').parentElement.scrollHeight;
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {hcp_hostname: $('hcp-hostname').value, hcp_organization: $('hcp-organization').value, hcp_workspace: $('hcp-workspace').value, hcp_submission_enabled: $('hcp-submission-enabled').checked, gcp_project_id: $('gcp-project-id').value, ai_provider: $('ai-provider').value};
  try {
    settings = await api('/api/settings', {method: 'PUT', body: JSON.stringify(payload)});
    workflow = null;
    currentScreen = 0;
    renderSettings();
    renderWizard();
    await loadProjects();
    showSuccess(settings.readiness.ready ? 'Production integrations saved. Infrastructure adoption is ready.' : 'Production integrations saved. Complete the missing readiness checks before loading data.');
  } catch (error) { showError(error); }
}

async function saveAiCredential() {
  const provider = $('ai-provider').value;
  const credential = $('ai-credential').value;
  if (!credential) {
    showError(new Error('Enter a credential before saving.'));
    return;
  }
  setCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/ai-credential', {
      method: 'PUT',
      body: JSON.stringify({provider, credential, activate: true}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('ai-credential').value = '';
    renderSettings();
    showSuccess(`${provider === 'ibm-bob' ? 'IBM Bob' : 'GitHub Copilot'} credential encrypted, saved, and selected. Test the connection before generation.`);
  } catch (error) {
    showError(error);
  } finally {
    setCredentialBusy(false);
  }
}

async function testGcpConnection() {
  const projectId = $('gcp-project-id').value.trim();
  if (!projectId) {
    showError(new Error('Enter and save a GCP project ID before testing.'));
    return;
  }
  if (projectId !== settings.gcp_project_id) {
    showError(new Error('Save Configuration before testing the changed GCP project ID.'));
    return;
  }
  setBusy($('gcp-connection-test'), true);
  clearMessages();
  try {
    const result = await api('/api/settings/gcp-connection/test', {
      method: 'POST',
      body: JSON.stringify({project_id: projectId}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    renderSettings();
    showSuccess(`GCP connection verified for ${result.project_id} as ${result.account}. Cloud Asset Inventory is readable.`);
  } catch (error) {
    try {
      settings = await api('/api/settings');
      renderSettings();
    } catch (_) {
      // Keep the original connection error when settings cannot be refreshed.
    }
    showError(error);
  } finally {
    setBusy($('gcp-connection-test'), false);
    if (settings) renderGcpConnection();
  }
}

async function saveAndTestGcpConnection() {
  const projectId = $('gcp-project-id').value.trim();
  const credential = $('gcp-credential').value;
  const configured = Boolean(settings.gcp_credential?.configured);
  if (!projectId) {
    showError(new Error('Enter a GCP project ID before saving.'));
    return;
  }
  if (!credential.trim() && !configured) {
    showError(new Error('Choose the Application Default Credential JSON file or paste its contents before saving.'));
    return;
  }
  setGcpCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/gcp-connection/configure', {
      method: 'POST',
      body: JSON.stringify({project_id: projectId, credential}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    clearGcpCredentialInputs();
    renderSettings();
    showSuccess(`GCP project and credentials saved and verified as ${result.account}. Cloud Asset Inventory is readable.`);
  } catch (error) {
    try {
      settings = await api('/api/settings');
      renderSettings();
    } catch (_) {
      // Keep the original configuration error when settings cannot be refreshed.
    }
    showError(error);
  } finally {
    setGcpCredentialBusy(false);
  }
}

async function loadGcpCredentialFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  if (file.size > 131072) {
    event.target.value = '';
    showError(new Error('GCP credential JSON exceeds the 128 KiB limit.'));
    return;
  }
  try {
    const credential = await file.text();
    const payload = JSON.parse(credential);
    if (!['authorized_user', 'service_account', 'external_account'].includes(payload.type)) {
      throw new Error('Credential type must be authorized_user, service_account, or external_account.');
    }
    $('gcp-credential').value = credential;
    $('gcp-credential-file-note').textContent = `${file.name} selected · ${payload.type}. The file will be encrypted only after Save & test.`;
    renderGcpConnection();
    clearMessages();
  } catch (error) {
    event.target.value = '';
    $('gcp-credential').value = '';
    showError(new Error(`Unable to read ADC JSON: ${error.message}`));
  }
}

async function clearGcpCredential() {
  const status = settings.gcp_credential || {};
  if (!status.managed) {
    showError(new Error('This credential is managed by the server environment and cannot be cleared in the UI.'));
    return;
  }
  setGcpCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/gcp-credential', {
      method: 'DELETE',
      body: '{}',
    });
    settings = {...result.settings, persistence: settings.persistence};
    clearGcpCredentialInputs();
    renderSettings();
    showSuccess('Application-managed GCP credentials cleared.');
  } catch (error) {
    showError(error);
  } finally {
    setGcpCredentialBusy(false);
  }
}

async function resetGcpConnection() {
  const status = settings.gcp_credential || {};
  if (status.configured && !status.managed) {
    showError(new Error('Deployment-managed credentials must be removed from the server environment before resetting this connection.'));
    return;
  }
  setGcpCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/gcp-credential', {
      method: 'DELETE',
      body: JSON.stringify({clear_project: true}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    clearGcpCredentialInputs();
    renderSettings();
    showSuccess('GCP credentials, project selection, and verification were reset.');
  } catch (error) {
    showError(error);
  } finally {
    setGcpCredentialBusy(false);
  }
}

function clearGcpCredentialInputs() {
  $('gcp-credential').value = '';
  $('gcp-credential-file').value = '';
  $('gcp-credential-file-note').textContent = 'The selected file remains in the browser until you choose Save & test.';
}

function setGcpCredentialBusy(busy) {
  $('gcp-project-id').disabled = busy;
  $('gcp-credential').disabled = busy;
  $('gcp-credential-file').disabled = busy;
  $('gcp-credential-save').disabled = busy;
  $('gcp-credential-clear').disabled = busy;
  $('gcp-connection-reset').disabled = busy;
  $('gcp-connection-test').disabled = busy;
  if (!busy && settings) renderGcpConnection();
}

function renderGcpConnection() {
  const status = settings.gcp_credential || {};
  const verified = Boolean(
    settings.gcp_verified_at
    && settings.gcp_verified_project_id === settings.gcp_project_id
  );
  $('gcp-credential').placeholder = status.configured ? 'Paste new JSON to replace the configured credential' : 'Paste service_account, authorized_user, or external_account JSON';
  const projectChanged = $('gcp-project-id').value.trim() !== settings.gcp_project_id;
  $('gcp-connection-status').textContent = verified ? 'Verified' : status.configured ? status.source === 'encrypted-store' ? 'Saved · not verified' : 'Environment managed' : settings.gcp_project_id ? 'Project saved · credentials missing' : 'Not configured';
  $('gcp-connection-status').classList.toggle('ok', verified || Boolean(status.configured));
  $('gcp-credential-save').disabled = !$('gcp-project-id').value.trim() || (!status.configured && !$('gcp-credential').value.trim());
  $('gcp-connection-test').disabled = projectChanged || !settings.gcp_project_id || !status.configured;
  $('gcp-credential-clear').disabled = !status.managed;
  $('gcp-connection-reset').disabled = !settings.gcp_project_id && !status.managed;
  $('gcp-connection-note').textContent = verified
    ? `Verified ${new Date(settings.gcp_verified_at).toLocaleString()} as ${settings.gcp_verified_account}.`
    : projectChanged
      ? 'Project or credentials changed. Use Save & test to persist and verify them together.'
    : status.source === 'environment' || status.source === 'environment-file'
      ? 'Credentials are deployment-managed. Save the project configuration, then test project and Cloud Asset Inventory access.'
      : status.configured
        ? 'Credentials are saved but not verified. Use Retest connection.'
        : 'Choose the ADC JSON file or paste it, then use Save & test connection.';
}

async function testAiCredential() {
  const provider = $('ai-provider').value;
  setCredentialBusy(true);
  clearMessages();
  try {
    await api('/api/settings/ai-credential/test', {
      method: 'POST',
      body: JSON.stringify({provider}),
    });
    settings = await api('/api/settings');
    renderSettings();
    showSuccess('Agent authentication verified with a minimal non-interactive request.');
  } catch (error) {
    showError(error);
  } finally {
    setCredentialBusy(false);
  }
}

async function clearAiCredential() {
  const provider = $('ai-provider').value;
  const status = settings.ai_credentials?.[provider];
  if (status?.source === 'environment') {
    showError(new Error('This credential is managed by the server environment and cannot be cleared in the UI.'));
    return;
  }
  setCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/ai-credential', {
      method: 'DELETE',
      body: JSON.stringify({provider}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('ai-credential').value = '';
    renderSettings();
    showSuccess('Application-managed AI credential cleared.');
  } catch (error) {
    showError(error);
  } finally {
    setCredentialBusy(false);
  }
}

function setCredentialBusy(busy) {
  $('ai-credential').disabled = busy;
  $('ai-credential-save').disabled = busy;
  $('ai-credential-test').disabled = busy;
  $('ai-credential-clear').disabled = busy;
  if (!busy && settings) renderAiCredential();
}

function renderAiCredential() {
  const provider = $('ai-provider').value;
  const copilot = provider === 'github-copilot';
  const status = settings.ai_credentials?.[provider] || {};
  $('ai-credential-label').textContent = copilot ? 'GitHub Copilot token' : 'IBM Bob API key';
  $('ai-credential').placeholder = status.configured ? 'Enter a new value to replace the stored credential' : copilot ? 'COPILOT_GITHUB_TOKEN' : 'BOBSHELL_API_KEY';
  $('ai-credential-status').textContent = status.configured ? status.verified_at ? 'Verified' : status.source === 'environment' ? 'Environment managed' : 'Configured' : 'Not configured';
  $('ai-credential-status').classList.toggle('ok', Boolean(status.configured));
  $('ai-credential-test').disabled = !status.configured;
  $('ai-credential-clear').disabled = !status.managed;
  $('ai-credential-note').textContent = status.source === 'environment'
    ? 'Managed by the server environment. Replace it here to move to encrypted application storage.'
    : status.verified_at
      ? `Last verified ${new Date(status.verified_at).toLocaleString()}. Testing uses one minimal agent request.`
      : 'The secret is never returned to this browser. Testing uses one minimal agent request.';
}

async function saveHcpCredential() {
  const credential = $('hcp-credential').value;
  if (!credential) {
    showError(new Error('Enter an HCP Terraform token before saving.'));
    return;
  }
  setHcpCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/hcp-credential', {
      method: 'PUT',
      body: JSON.stringify({credential}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('hcp-credential').value = '';
    renderSettings();
    showSuccess('HCP Terraform token encrypted and saved. Test the connection before using a real workflow.');
  } catch (error) {
    showError(error);
  } finally {
    setHcpCredentialBusy(false);
  }
}

async function testHcpCredential() {
  setHcpCredentialBusy(true);
  clearMessages();
  try {
    await api('/api/settings/hcp-credential/test', {
      method: 'POST',
      body: '{}',
    });
    settings = await api('/api/settings');
    renderSettings();
    showSuccess('HCP Terraform authentication verified with a read-only account request.');
  } catch (error) {
    showError(error);
  } finally {
    setHcpCredentialBusy(false);
  }
}

async function clearHcpCredential() {
  const status = settings.hcp_credential || {};
  if (status.source === 'environment') {
    showError(new Error('This token is managed by the server environment and cannot be cleared in the UI.'));
    return;
  }
  setHcpCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/hcp-credential', {
      method: 'DELETE',
      body: '{}',
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('hcp-credential').value = '';
    renderSettings();
    showSuccess('Application-managed HCP Terraform token cleared.');
  } catch (error) {
    showError(error);
  } finally {
    setHcpCredentialBusy(false);
  }
}

function setHcpCredentialBusy(busy) {
  $('hcp-credential').disabled = busy;
  $('hcp-credential-save').disabled = busy;
  $('hcp-credential-test').disabled = busy;
  $('hcp-credential-clear').disabled = busy;
  if (!busy && settings) renderHcpCredential();
}

function renderHcpCredential() {
  const status = settings.hcp_credential || {};
  $('hcp-credential').placeholder = status.configured ? 'Enter a new token to replace the stored value' : 'Paste a user or team token';
  $('hcp-credential-status').textContent = status.configured ? status.verified_at ? 'Verified' : status.source === 'environment' ? 'Environment managed' : 'Configured' : 'Not configured';
  $('hcp-credential-status').classList.toggle('ok', Boolean(status.configured));
  $('hcp-credential-test').disabled = !status.configured;
  $('hcp-credential-clear').disabled = !status.managed;
  $('hcp-credential-note').textContent = status.source === 'environment'
    ? 'Managed by the server environment. Replace it here to move to encrypted application storage.'
    : status.verified_at
      ? `Last verified ${new Date(status.verified_at).toLocaleString()}. Testing performs one read-only account request.`
      : 'The token is never returned to this browser. Testing performs one read-only account request.';
}

async function saveGithubCredential() {
  const credential = $('github-credential').value;
  if (!credential) {
    showError(new Error('Paste a GitHub API token before saving.'));
    return;
  }
  setGithubCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/github-credential', {
      method: 'PUT',
      body: JSON.stringify({credential}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('github-credential').value = '';
    renderSettings();
    showSuccess('GitHub API token encrypted. Test the connection before creating pull requests.');
  } catch (error) {
    showError(error);
  } finally {
    setGithubCredentialBusy(false);
  }
}

async function testGithubCredential() {
  setGithubCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/github-credential/test', {
      method: 'POST',
      body: '{}',
    });
    settings = {...result.settings, persistence: settings.persistence};
    renderSettings();
    showSuccess(`GitHub API authentication verified for ${result.account}.`);
  } catch (error) {
    showError(error);
  } finally {
    setGithubCredentialBusy(false);
  }
}

async function clearGithubCredential() {
  const status = settings.github_credential || {};
  if (status.source === 'environment') {
    showError(new Error('This token is managed by the server environment and cannot be cleared in the UI.'));
    return;
  }
  setGithubCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/github-credential', {
      method: 'DELETE',
      body: '{}',
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('github-credential').value = '';
    renderSettings();
    showSuccess('Application-managed GitHub API token cleared.');
  } catch (error) {
    showError(error);
  } finally {
    setGithubCredentialBusy(false);
  }
}

function setGithubCredentialBusy(busy) {
  $('github-credential').disabled = busy;
  $('github-credential-save').disabled = busy;
  $('github-credential-test').disabled = busy;
  $('github-credential-clear').disabled = busy;
  if (!busy && settings) renderGithubCredential();
}

function renderGithubCredential() {
  const status = settings.github_credential || {};
  $('github-credential').placeholder = status.configured ? 'Enter a new token to replace the stored value' : 'Paste a fine-grained personal access token';
  $('github-credential-status').textContent = status.configured ? status.verified_at ? 'Verified' : status.source === 'environment' ? 'Environment managed' : 'Configured' : 'Not configured';
  $('github-credential-status').classList.toggle('ok', Boolean(status.configured));
  $('github-credential-test').disabled = !status.configured;
  $('github-credential-clear').disabled = !status.managed;
  $('github-credential-note').textContent = status.source === 'environment'
    ? 'Managed by the server environment. Replace it here to move to encrypted application storage.'
    : status.verified_at
      ? `Last verified ${new Date(status.verified_at).toLocaleString()}. The token can authenticate HTTPS Git and create pull requests.`
      : 'The token is never returned to this browser. Testing performs one read-only GitHub account request.';
}

async function saveGitSshCredential() {
  const credential = $('git-ssh-credential').value;
  const repository = $('git-ssh-test-repository').value.trim();
  if (!credential) {
    showError(new Error('Paste a Git SSH private key before saving.'));
    return;
  }
  if (!repository) {
    showError(new Error('Enter an SSH repository URL so the saved key can be verified.'));
    return;
  }
  setGitSshCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/git-ssh-credential', {
      method: 'PUT',
      body: JSON.stringify({credential, repository}),
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('git-ssh-credential').value = '';
    renderSettings();
    showSuccess(`Git SSH private key encrypted and repository access verified (${result.connection.fingerprint}).`);
  } catch (error) {
    showError(error);
  } finally {
    setGitSshCredentialBusy(false);
  }
}

async function testGitSshCredential() {
  const repository = $('git-ssh-test-repository').value.trim();
  if (!repository) {
    showError(new Error('Enter an SSH repository URL for the connection test.'));
    return;
  }
  setGitSshCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/git-ssh-credential/test', {
      method: 'POST',
      body: JSON.stringify({repository}),
    });
    settings = await api('/api/settings');
    renderSettings();
    showSuccess(`Git SSH repository access verified (${result.fingerprint}).`);
  } catch (error) {
    showError(error);
  } finally {
    setGitSshCredentialBusy(false);
  }
}

async function clearGitSshCredential() {
  const status = settings.git_ssh_credential || {};
  if (status.source === 'environment') {
    showError(new Error('This key is managed by the server environment and cannot be cleared in the UI.'));
    return;
  }
  setGitSshCredentialBusy(true);
  clearMessages();
  try {
    const result = await api('/api/settings/git-ssh-credential', {
      method: 'DELETE',
      body: '{}',
    });
    settings = {...result.settings, persistence: settings.persistence};
    $('git-ssh-credential').value = '';
    renderSettings();
    showSuccess('Application-managed Git SSH private key cleared.');
  } catch (error) {
    showError(error);
  } finally {
    setGitSshCredentialBusy(false);
  }
}

function setGitSshCredentialBusy(busy) {
  $('git-ssh-credential').disabled = busy;
  $('git-ssh-test-repository').disabled = busy;
  $('git-ssh-credential-save').disabled = busy;
  $('git-ssh-credential-test').disabled = busy;
  $('git-ssh-credential-clear').disabled = busy;
  if (!busy && settings) renderGitSshCredential();
}

function renderGitSshCredential() {
  const status = settings.git_ssh_credential || {};
  $('git-ssh-credential').placeholder = status.configured ? 'Paste a new key to replace the stored value' : 'Paste an OpenSSH or PEM private key';
  $('git-ssh-credential-status').textContent = status.configured ? status.verified_at ? 'Verified' : status.source === 'environment' ? 'Environment managed' : 'Configured' : 'Not configured';
  $('git-ssh-credential-status').classList.toggle('ok', Boolean(status.configured));
  $('git-ssh-credential-test').disabled = !status.configured;
  $('git-ssh-credential-clear').disabled = !status.managed;
  $('git-ssh-credential-note').textContent = status.source === 'environment'
    ? 'Managed by the server environment. Replace it here to move to encrypted application storage.'
    : status.verified_at
      ? `Last verified ${new Date(status.verified_at).toLocaleString()} against the supplied repository.`
      : 'The key is never returned to this browser. Test access against an SSH repository before delivery.';
}

function renderSettings() {
  $('hcp-hostname').value = settings.hcp_hostname;
  $('hcp-organization').value = settings.hcp_organization;
  $('hcp-workspace').value = settings.hcp_workspace;
  $('hcp-submission-enabled').checked = settings.hcp_submission_enabled !== false;
  $('gcp-project-id').value = settings.gcp_project_id;
  $('ai-provider').value = settings.ai_provider;
  renderHcpCredential();
  renderAiCredential();
  renderGithubCredential();
  renderGitSshCredential();
  renderGcpConnection();
  const adoptionReady = settings.readiness.ready;
  $('readiness').textContent = adoptionReady ? 'Production · ready' : 'Production · setup required';
  $('sidebar-mode').textContent = 'Production integrations';
  $('sidebar-detail').textContent = adoptionReady ? 'Adoption ready' : 'Configuration incomplete';
  $('sidebar-dot').classList.toggle('warning-dot', !adoptionReady);
  $('source-tag').textContent = 'Cloud Asset Inventory';
  $('hcp-target').textContent = settings.hcp_organization && settings.hcp_workspace ? `${settings.hcp_organization} / ${settings.hcp_workspace}` : 'Not configured';
  const checks = [
    ['HCP API token', settings.readiness.hcp_token, 'save it here or provide TERRAMIG_HCP_TOKEN'],
    ['HCP authentication', settings.readiness.hcp_authenticated, 'run Test connection for the HCP token'],
    ['Adoption workspace', settings.readiness.adoption_target, 'organization and workspace configured'],
    ['Google Cloud CLI', settings.readiness.gcloud, 'gcloud installed'],
    ['GCP credentials', settings.readiness.gcp_credential, 'save JSON here or mount Application Default Credentials'],
    ['GCP authentication', settings.readiness.gcp_authenticated, 'save the project and run Test GCP connection'],
    [settings.ai_provider === 'github-copilot' ? 'GitHub Copilot CLI' : 'IBM Bob Shell', settings.readiness.ai_installed, `${settings.readiness.ai_binary} installed`],
    ['AI credential', settings.readiness.ai_credential, 'save or provide the selected provider credential'],
    ['AI authentication', settings.readiness.ai_authenticated, 'run Test connection for the selected credential'],
    ['Terraform CLI', settings.readiness.terraform, 'terraform installed for the local no-drift gate'],
    ['Terraform MCP server', settings.readiness.terraform_mcp, 'terraform-mcp-server installed for live private-module context'],
    ['Git CLI', settings.readiness.git, 'git installed for model and target repository delivery'],
    ['GitHub pull-request API', settings.readiness.github_api, 'save and test a GitHub token here or provide TERRAMIG_GITHUB_TOKEN'],
    ['Git SSH private key', settings.readiness.git_ssh_key, 'save a key here or provide TERRAMIG_GIT_SSH_PRIVATE_KEY'],
    ['Git SSH repository access', settings.readiness.git_ssh_authenticated, 'run Test repository access for the stored key'],
    ['Git authentication', settings.readiness.git_auth, 'verified SSH key, verified GitHub token, or a forwarded SSH agent'],
    ['HCP saved-plan submission', settings.readiness.hcp_submission, 'enable protected submission above'],
  ];
  $('readiness-list').innerHTML = checks.map(([label, ready, hint]) => `<div class="check"><i class="${ready ? 'ok' : ''}">${ready ? '✓' : '!'}</i><div><strong>${label}</strong><small>${ready ? 'Available' : hint}</small></div></div>`).join('');
  configureNextButton();
}

function renderWizard() {
  document.querySelectorAll('.wizard-screen').forEach((screen, index) => screen.classList.toggle('hidden', index !== currentScreen));
  $('project').disabled = Boolean(workflow);
  const accessible = maxAccessibleScreen();
  const adoptionTerminal = ['submitted', 'imported'].includes(workflow?.stage);
  document.querySelectorAll('.adoption-step').forEach((step, index) => {
    step.classList.toggle('current', index === currentScreen);
    step.classList.toggle('done', index < accessible || (adoptionTerminal && index === 6));
    step.disabled = wizardBusy || index > accessible;
    step.setAttribute('aria-current', index === currentScreen ? 'step' : 'false');
    step.querySelector('i').textContent = index < accessible || (adoptionTerminal && index === 6) ? '✓' : index + 1;
  });
  $('wizard-previous').disabled = wizardBusy || currentScreen === 0;
  $('wizard-position').textContent = `Step ${currentScreen + 1} of ${screenContent.length}`;
  $('wizard-state').textContent = workflow ? `Run state · ${workflow.stage}` : 'Scope not started';
  configureNextButton();
  $('events').innerHTML = workflow?.events?.length ? workflow.events.map(event => `<div class="event">${escapeHtml(event)}</div>`).join('') : '<div class="empty">Start an adoption run to see each decision and gate.</div>';
  if (workflow?.resources?.length) renderWorkflowResources();
  if (workflow?.candidates && Object.keys(workflow.candidates).length) renderModuleMatches();
  renderCompositionProgress();
  renderJobTerminal('discover', 'discovery', 'Start discovery to open the Cloud Asset session.');
  renderJobTerminal('validate', 'verification', 'Start local verification to open the Terraform session.');
  renderJobTerminal('deliver', 'git', 'Configure repositories and start delivery to open the Git session.');
  renderHcpTerminal();
  $('code-panel').classList.toggle('hidden', !workflow?.bundle);
  $('composition-panel').classList.toggle('hidden', !workflow?.bundle);
  if (workflow?.bundle) renderComposition();
  if (workflow?.verification) renderVerification();
  renderGitDelivery();
  renderImportReview();
  $('download-workflow-report').disabled = !workflow;
}

async function waitForJob(initialJob) {
  let job = initialJob;
  activeJob = job;
  captureJobLogs(job);
  while (['queued', 'running'].includes(job.status)) {
    const state = `${job.action} · ${job.status} · attempt ${job.attempts || 0}/${job.max_attempts}`;
    $('wizard-state').textContent = state;
    if (job.kind === 'adoption' && workflow?.id === job.subject_id) {
      try {
        workflow = await api(`/api/workflows/${workflow.id}`);
        renderWizard();
        $('wizard-state').textContent = state;
      } catch (_) {
        // Job polling remains authoritative if a transient progress refresh fails.
      }
    }
    await new Promise(resolve => setTimeout(resolve, 750));
    job = await api(`/api/jobs/${job.id}`);
    activeJob = job;
    captureJobLogs(job);
    renderWizard();
  }
  if (job.kind === 'adoption' && workflow?.id === job.subject_id) {
    try { workflow = await api(`/api/workflows/${workflow.id}`); } catch (_) { /* Keep the job result visible. */ }
    renderWizard();
  }
  if (job.status === 'failed') throw new Error(job.last_error || `${job.action} failed`);
  if (job.status === 'cancelled') throw new Error(`${job.action} was cancelled`);
  return job;
}

async function monitorHcpImport(workflowId) {
  if (lifecycleMonitors.has(workflowId)) return;
  lifecycleMonitors.add(workflowId);
  try {
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 4000));
      const latest = await api(`/api/workflows/${workflowId}`);
      if (workflow?.id === workflowId) {
        workflow = latest;
        renderWizard();
      }
      if (latest.stage === 'imported') {
        showSuccess(`HCP import ${latest.import_run_id} completed and the post-import plan is drift-free.`);
        return;
      }
      if (['failed', 'post-import-blocked', 'drift-detected'].includes(latest.hcp_import?.status)) {
        showError(new Error(latest.hcp_import.diagnostics?.at(-1) || `HCP lifecycle ${latest.hcp_import.status}`));
        return;
      }
      const jobs = await api('/api/jobs');
      jobs.jobs.filter(job => job.subject_id === workflowId).forEach(captureJobLogs);
      if (workflow?.id === workflowId) renderWizard();
      const active = jobs.jobs.some(job => job.subject_id === workflowId && job.action === 'reconcile' && ['queued', 'running'].includes(job.status));
      if (!active) return;
    }
  } catch (error) {
    if (workflow?.id === workflowId) showError(error);
  } finally {
    lifecycleMonitors.delete(workflowId);
  }
}

function configureNextButton() {
  const labels = ['Discover resources →', 'Match selected resources →', 'Compose selected representations →', 'Run local verification →', 'Continue to Git delivery →', 'Deliver to target repository →', 'Simulate HCP import'];
  let label = labels[currentScreen];
  let disabled = wizardBusy;
  if (wizardBusy && activeJob?.action === 'generate') label = 'Agent working asynchronously…';
  else if (wizardBusy) label = 'Working…';
  else if (currentScreen === 3 && workflow?.stage === 'matched') label = 'Retry AI composition →';
  else if (currentScreen === 4 && driftAcceptanceEligible()) {
    label = $('accept-drift').checked ? 'Accept drift and continue →' : 'Confirm drift acceptance';
    disabled = !$('accept-drift').checked;
  }
  else if (currentScreen === 4 && workflow?.stage === 'generated') label = 'Retry local verification';
  else if (currentScreen === 5 && ['branch-pushed', 'pull-request-open'].includes(workflow?.git_delivery?.status)) label = 'Continue to HCP import →';
  else if (currentScreen === 5 && $('git-create-pr').checked && !settings.readiness.github_api) { label = 'Configure GitHub token or push branch only'; disabled = true; }
  else if (currentScreen === 6 && workflow?.stage === 'submitted') { label = 'Monitoring HCP apply…'; disabled = true; }
  else if (currentScreen === 6 && workflow?.stage === 'imported') { label = 'Import and zero-drift check completed'; disabled = true; }
  else if (currentScreen === 6 && !settings.readiness.hcp_submission) { label = 'Enable HCP saved-plan submission'; disabled = true; }
  else if (currentScreen === 6 && !$('adoption-confirm').checked) { label = 'Authorize HCP submission'; disabled = true; }
  else if (currentScreen === 6) label = 'Submit saved plan to HCP';
  else if (currentScreen === 1 && workflow && !workflow.selected_resource_ids?.length) { label = 'Select at least one resource'; disabled = true; }
  else if (currentScreen > maxAccessibleScreen()) disabled = true;
  $('wizard-next').textContent = label;
  $('wizard-next').disabled = disabled;
}

function driftAcceptanceEligible() {
  return Boolean(
    workflow?.stage === 'generated'
    && workflow.verification?.drift_detected
    && workflow.bundle
    && workflow.verification.planned_imports === workflow.bundle.imports.length
  );
}

function renderGitDelivery() {
  const delivery = workflow?.git_delivery;
  const complete = ['branch-pushed', 'pull-request-open'].includes(delivery?.status);
  $('git-delivery-badge').textContent = delivery?.status || 'Pending';
  $('git-delivery-badge').className = `verification-badge ${complete ? 'pass' : delivery?.status === 'failed' ? 'fail' : ''}`;
  hydrateGitDeliveryForm(delivery);
  ['git-model-repository', 'git-target-repository', 'git-model-ref', 'git-base-branch', 'git-delivery-branch', 'git-target-path', 'git-create-pr', 'git-update-existing-directory', 'git-allow-overwrite'].forEach(id => $(id).disabled = complete || wizardBusy);
  $('git-delivery-result').classList.toggle('hidden', !delivery);
  if (!delivery) return;
  $('git-result-branch').textContent = delivery.branch || '—';
  $('git-result-commit').textContent = delivery.commit_sha ? delivery.commit_sha.slice(0, 12) : '—';
  $('git-result-files').textContent = `${delivery.changed_files?.length || 0} changed · ${delivery.skipped_template_files?.length || 0} preserved`;
  $('git-result-output').textContent = delivery.output || 'Git delivery is pending.';
  $('git-result-pr').classList.toggle('hidden', !delivery.pull_request_url);
  if (delivery.pull_request_url) $('git-result-pr').href = delivery.pull_request_url;
}

function readGitDeliveryForm() {
  return {
    model_repository: $('git-model-repository').value.trim(),
    target_repository: $('git-target-repository').value.trim(),
    model_ref: $('git-model-ref').value.trim(),
    base_branch: $('git-base-branch').value.trim(),
    branch: $('git-delivery-branch').value.trim(),
    target_path: $('git-target-path').value.trim(),
    create_pull_request: $('git-create-pr').checked,
    update_existing_directory: $('git-update-existing-directory').checked,
    allow_overwrite: $('git-allow-overwrite').checked,
  };
}

function hydrateGitDeliveryForm(delivery) {
  const workflowId = workflow?.id || '';
  if (gitDeliveryFormWorkflowId === workflowId) return;
  gitDeliveryFormWorkflowId = workflowId;
  if (!delivery) return;
  $('git-model-repository').value = delivery.model_repository;
  $('git-target-repository').value = delivery.target_repository;
  $('git-model-ref').value = delivery.model_ref;
  $('git-base-branch').value = delivery.base_branch;
  $('git-delivery-branch').value = delivery.branch;
  $('git-target-path').value = delivery.target_path;
  $('git-create-pr').checked = delivery.create_pull_request;
  $('git-update-existing-directory').checked = delivery.update_existing_directory;
  $('git-allow-overwrite').checked = delivery.allow_overwrite;
}

function renderWorkflowResources() {
  const supported = workflow.resources.filter(resource => resource.support_status === 'supported').length;
  const hidden = workflow.resources.length - supported;
  const selected = new Set(workflow.selected_resource_ids || workflow.ordered_resource_ids);
  const tracked = workflow.managed_resources || {};
  const resourcesById = new Map(workflow.resources.map(resource => [resource.id, resource]));
  const visibleResources = workflow.ordered_resource_ids
    .map(id => resourcesById.get(id))
    .filter(Boolean)
    .filter(resource => resourceVisibility === 'all' || resource.support_status === 'supported')
    .filter(resource => {
      const managed = Boolean(tracked[resource.id]);
      return resourceManagementVisibility === 'all'
        || (resourceManagementVisibility === 'tracked' ? managed : !managed);
    })
    .filter(resource => resourceMatchesSearch(resource, resourceSearch));
  $('resource-visibility').value = resourceVisibility;
  $('resource-management').value = resourceManagementVisibility;
  $('resource-grouping').value = resourceGrouping;
  $('resource-search').value = resourceSearch;
  const reviewLabel = resourceVisibility === 'supported' ? `${hidden} hidden` : `${hidden} need review`;
  const trackedCount = Object.keys(tracked).length;
  $('resource-count').textContent = `${workflow.resources.length} deployed resources · ${supported} supported${trackedCount ? ` · ${trackedCount} previously imported` : ''}${hidden ? ` · ${reviewLabel}` : ''}`;
  $('resource-selection-count').textContent = `${selected.size} selected · ${visibleResources.length} shown`;
  const groups = groupResources(visibleResources, resourceGrouping);
  $('resources').innerHTML = groups.length
    ? groups.map(([key, resources]) => renderResourceGroup(key, resources, selected, tracked)).join('')
    : '<div class="empty">No resources match these filters. Change the support, management, or search filters to broaden the review.</div>';
}

function renderModuleMatches() {
  const selectedIds = workflow.selected_resource_ids || workflow.ordered_resource_ids;
  const resourcesById = new Map(workflow.resources.map(resource => [resource.id, resource]));
  const visibleResources = selectedIds
    .map(id => resourcesById.get(id))
    .filter(Boolean)
    .filter(resource => resourceMatchesSearch(resource, matchSearch));
  const selectedModules = selectedIds.filter(id => workflow.module_selections?.[id] && workflow.module_selections[id] !== '__resource__').length;
  const compatibleModules = selectedIds.filter(id => {
    const resource = resourcesById.get(id);
    return (workflow.candidates[id] || []).some(candidate => isSingleResourceCandidate(resource, candidate));
  }).length;
  const compositeOnly = selectedIds.filter(id => {
    const resource = resourcesById.get(id);
    const candidates = workflow.candidates[id] || [];
    return candidates.length && !candidates.some(candidate => isSingleResourceCandidate(resource, candidate));
  }).length;
  $('match-count').textContent = `${selectedModules} modules selected · ${selectedIds.length - selectedModules} direct`;
  $('module-bulk-status').textContent = `${compatibleModules} latest compatible module matches · ${selectedIds.length - compatibleModules} direct fallbacks${compositeOnly ? ` · ${compositeOnly} composite-only matches require review` : ''}`;
  $('select-latest-compatible-modules').disabled = wizardBusy || workflow.stage !== 'matched';
  $('select-direct-resources').disabled = wizardBusy || workflow.stage !== 'matched';
  $('public-registry-status').textContent = workflow.public_registry_searched ? 'Search complete · verified publishers and single-resource module units only' : 'Not searched · verified publishers only';
  $('search-public-modules').disabled = wizardBusy || workflow.stage !== 'matched';
  $('search-public-modules').textContent = workflow.public_registry_searched ? 'Refresh public search' : 'Search public modules';
  $('match-grouping').value = matchGrouping;
  $('match-search').value = matchSearch;
  const groups = groupMatchResources(visibleResources, matchGrouping);
  $('match-list').innerHTML = groups.length ? groups.map(([key, resources]) => {
    const moduleCount = resources.filter(resource => workflow.module_selections?.[resource.id] && workflow.module_selections[resource.id] !== '__resource__').length;
    const expanded = expandedMatchGroups.has(key) || matchSearch;
    const shown = expanded ? resources : resources.slice(0, 30);
    return `<section class="resource-group module-group"><div class="resource-group-header"><div><strong>${escapeHtml(key)}</strong><small>${resources.length} resources · ${moduleCount} module selections · ${resources.length - moduleCount} direct</small></div><div class="resource-group-actions"><button class="ghost compact" data-match-group-policy="latest-compatible" data-match-group-key="${escapeHtml(key)}">Latest compatible</button><button class="ghost compact" data-match-group-policy="direct" data-match-group-key="${escapeHtml(key)}">Use direct</button></div></div><div class="resource-group-items">${shown.map(resource => renderModuleMatchCard(resource)).join('')}</div>${shown.length < resources.length ? `<button class="group-more" data-expand-match-group="${escapeHtml(key)}">Show all ${resources.length} resources in this group</button>` : ''}</section>`;
  }).join('') : '<div class="empty">No selected resources match this search.</div>';
}

function renderModuleMatchCard(resource) {
    const id = resource.id;
    const candidates = workflow.candidates[id] || [];
    const selectedSource = workflow.module_selections?.[id] || candidates[0]?.module.source || '__resource__';
    const moduleOptions = candidates.map(candidate => {
      const module = candidate.module;
      const registryLabel = module.registry_kind === 'public' ? 'Public · verified publisher' : 'HCP private';
      const addresses = module.managed_resource_addresses?.length
        ? module.managed_resource_addresses
        : module.managed_resource_address ? [module.managed_resource_address] : [];
      const singleResource = isSingleResourceCandidate(resource, candidate);
      const compatibilityLabel = singleResource ? 'Single-resource candidate' : `Composite · ${addresses.length} managed`;
      const compatibilityClass = singleResource ? 'single' : 'composite';
      return `<label class="module-option"><input class="module-choice" type="radio" name="module-${escapeHtml(id)}" data-module-resource="${escapeHtml(id)}" value="${escapeHtml(module.source)}" ${selectedSource === module.source ? 'checked' : ''}><span><strong>${escapeHtml(module.source)} @ ${escapeHtml(module.version)}</strong><small>${Math.round(candidate.score * 100)}% match · ${escapeHtml(candidate.reasons.join(' · '))}</small></span><span class="registry-badge ${compatibilityClass}" title="${escapeHtml(registryLabel)}">${escapeHtml(compatibilityLabel)}</span></label>`;
    }).join('');
    const directOption = `<label class="module-option direct-fallback"><input class="module-choice" type="radio" name="module-${escapeHtml(id)}" data-module-resource="${escapeHtml(id)}" value="__resource__" ${selectedSource === '__resource__' ? 'checked' : ''}><span><strong>${escapeHtml(resource.terraform_type || 'No direct mapping')}</strong><small>Use the provider resource directly. This is the conservative brownfield fallback and preserves the deterministic import ID.</small></span><span class="registry-badge direct">Direct resource</span></label>`;
    const options = moduleOptions + directOption;
    return `<article class="module-match-card"><div class="module-match-head"><div class="resource-icon">${resourceGlyph(resource.type)}</div><div><strong>${escapeHtml(resource.name)}</strong><small>${escapeHtml(resource.type)}</small></div><button class="resource-details" data-open-resource data-resource-id="${escapeHtml(resource.id)}">View details →</button></div><div class="module-options">${options}</div></article>`;
}

function resourceMatchesSearch(resource, query) {
  if (!query) return true;
  return [resource.name, resource.type, resource.location, resource.terraform_type, resource.import_id]
    .some(value => String(value || '').toLowerCase().includes(query));
}

function resourceDomain(resource) {
  const service = String(resource.type || '').split('/')[0];
  const kind = String(resource.type || '').split('/').pop().toLowerCase();
  if (service === 'compute.googleapis.com') {
    return /(network|subnetwork|firewall|route|address|forwarding|backend|proxy|urlmap|vpn|router|ssl|healthcheck)/.test(kind)
      ? 'Network & load balancing'
      : 'Compute';
  }
  if (['container.googleapis.com', 'gkehub.googleapis.com'].includes(service)) return 'Containers & Kubernetes';
  if (['storage.googleapis.com', 'file.googleapis.com', 'backupdr.googleapis.com'].includes(service)) return 'Storage & backup';
  if (['sqladmin.googleapis.com', 'alloydb.googleapis.com', 'spanner.googleapis.com', 'firestore.googleapis.com', 'redis.googleapis.com'].includes(service)) return 'Databases';
  if (['bigquery.googleapis.com', 'pubsub.googleapis.com', 'dataflow.googleapis.com', 'dataproc.googleapis.com'].includes(service)) return 'Data & analytics';
  if (['run.googleapis.com', 'cloudfunctions.googleapis.com', 'appengine.googleapis.com', 'workflows.googleapis.com'].includes(service)) return 'Serverless & integration';
  if (['iam.googleapis.com', 'cloudkms.googleapis.com', 'secretmanager.googleapis.com', 'orgpolicy.googleapis.com'].includes(service)) return 'Identity & security';
  if (['monitoring.googleapis.com', 'logging.googleapis.com'].includes(service)) return 'Observability';
  if (['aiplatform.googleapis.com'].includes(service)) return 'AI & machine learning';
  return 'Other GCP services';
}

function resourceGroupKey(resource, mode) {
  if (mode === 'service') return String(resource.type || 'Unknown service').split('/')[0];
  if (mode === 'location') return resource.location || 'Global / unspecified';
  if (mode === 'support') return `${resource.support_status || 'unknown'} support`;
  return resourceDomain(resource);
}

function groupResources(resources, mode) {
  const groups = new Map();
  resources.forEach(resource => {
    const key = resourceGroupKey(resource, mode);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(resource);
  });
  return [...groups.entries()].sort((left, right) => left[0].localeCompare(right[0]));
}

function groupMatchResources(resources, mode) {
  if (mode !== 'decision') return groupResources(resources, mode);
  const groups = new Map();
  resources.forEach(resource => {
    const candidates = workflow.candidates[resource.id] || [];
    const selected = workflow.module_selections?.[resource.id] || '__resource__';
    const compatible = candidates.some(candidate => isSingleResourceCandidate(resource, candidate));
    const key = selected !== '__resource__'
      ? 'Module selected'
      : compatible ? 'Compatible module available'
        : candidates.length ? 'Composite module review'
          : 'Direct resource fallback';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(resource);
  });
  const order = ['Module selected', 'Compatible module available', 'Composite module review', 'Direct resource fallback'];
  return [...groups.entries()].sort((left, right) => order.indexOf(left[0]) - order.indexOf(right[0]));
}

function renderResourceGroup(key, resources, selected, tracked) {
  const expanded = expandedResourceGroups.has(key) || resourceSearch;
  const shown = expanded ? resources : resources.slice(0, 40);
  const selectedCount = resources.filter(resource => selected.has(resource.id)).length;
  const trackedCount = resources.filter(resource => tracked[resource.id]).length;
  const selectableCount = resources.filter(resource => resource.support_status === 'supported' && !tracked[resource.id]).length;
  return `<section class="resource-group"><div class="resource-group-header"><div><strong>${escapeHtml(key)}</strong><small>${resources.length} resources · ${selectedCount} selected${trackedCount ? ` · ${trackedCount} previously imported` : ''}</small></div><div class="resource-group-actions"><button class="ghost compact" data-resource-group-action="select" data-resource-group-key="${escapeHtml(key)}" ${selectableCount ? '' : 'disabled'}>Select group</button><button class="ghost compact" data-resource-group-action="clear" data-resource-group-key="${escapeHtml(key)}">Clear group</button></div></div><div class="resource-group-items">${shown.map(resource => renderResourceRow(resource, selected, tracked)).join('')}</div>${shown.length < resources.length ? `<button class="group-more" data-expand-resource-group="${escapeHtml(key)}">Show all ${resources.length} resources in this group</button>` : ''}</section>`;
}

function renderResourceRow(resource, selected, tracked) {
  const management = tracked[resource.id];
  const selectable = ['discovered', 'matched'].includes(workflow.stage) && resource.support_status === 'supported';
  const managementBadge = management
    ? `<span class="management-badge tracked" title="${escapeHtml(management.target || 'HCP Terraform')}">Previously imported</span>`
    : '<span class="management-badge">Not tracked</span>';
  return `<div class="resource selectable"><input class="resource-select" type="checkbox" data-select-resource="${escapeHtml(resource.id)}" aria-label="Select ${escapeHtml(resource.name)}" ${selectable && selected.has(resource.id) ? 'checked' : ''} ${selectable ? '' : 'disabled'}><div class="resource-icon">${resourceGlyph(resource.type)}</div><div><strong>${escapeHtml(resource.name)}</strong><small>${escapeHtml(resource.type)} · ${escapeHtml(resource.location)} · ${resource.dependency_ids.length} dependencies</small></div><div class="resource-coverage">${managementBadge}${supportBadge(resource)}<button class="resource-details" data-open-resource data-resource-id="${escapeHtml(resource.id)}">View details →</button></div></div>`;
}

function currentVisibleResources() {
  const tracked = workflow.managed_resources || {};
  return workflow.ordered_resource_ids
    .map(id => workflow.resources.find(resource => resource.id === id))
    .filter(Boolean)
    .filter(resource => resourceVisibility === 'all' || resource.support_status === 'supported')
    .filter(resource => resourceManagementVisibility === 'all'
      || (resourceManagementVisibility === 'tracked' ? Boolean(tracked[resource.id]) : !tracked[resource.id]))
    .filter(resource => resourceMatchesSearch(resource, resourceSearch));
}

function handleResourceGroupAction(event) {
  const expand = event.target.closest('[data-expand-resource-group]');
  if (expand) {
    expandedResourceGroups.add(expand.dataset.expandResourceGroup);
    renderWorkflowResources();
    return;
  }
  const button = event.target.closest('[data-resource-group-action]');
  if (!button || !workflow || !['discovered', 'matched'].includes(workflow.stage)) return;
  const tracked = workflow.managed_resources || {};
  const groupResources = currentVisibleResources().filter(resource => resourceGroupKey(resource, resourceGrouping) === button.dataset.resourceGroupKey);
  const selected = new Set(workflow.selected_resource_ids || []);
  groupResources.forEach(resource => {
    if (button.dataset.resourceGroupAction === 'clear') selected.delete(resource.id);
    else if (resource.support_status === 'supported' && !tracked[resource.id]) selected.add(resource.id);
  });
  workflow.selected_resource_ids = workflow.ordered_resource_ids.filter(id => selected.has(id));
  renderWorkflowResources();
  configureNextButton();
}

function handleMatchGroupAction(event) {
  const expand = event.target.closest('[data-expand-match-group]');
  if (expand) {
    expandedMatchGroups.add(expand.dataset.expandMatchGroup);
    renderModuleMatches();
    return;
  }
  const button = event.target.closest('[data-match-group-policy]');
  if (!button || !workflow || workflow.stage !== 'matched') return;
  workflow.module_selections ||= {};
  const resourcesById = new Map(workflow.resources.map(resource => [resource.id, resource]));
  const visible = (workflow.selected_resource_ids || []).map(id => resourcesById.get(id)).filter(Boolean).filter(resource => resourceMatchesSearch(resource, matchSearch));
  const group = groupMatchResources(visible, matchGrouping).find(([key]) => key === button.dataset.matchGroupKey);
  (group?.[1] || []).forEach(resource => {
    if (button.dataset.matchGroupPolicy === 'direct') {
      workflow.module_selections[resource.id] = '__resource__';
      return;
    }
    const candidate = (workflow.candidates[resource.id] || []).find(item => isSingleResourceCandidate(resource, item));
    workflow.module_selections[resource.id] = candidate?.module.source || '__resource__';
  });
  renderModuleMatches();
}

function isSingleResourceCandidate(resource, candidate) {
  if (!resource || !candidate?.module) return false;
  const module = candidate.module;
  const addresses = module.managed_resource_addresses?.length
    ? module.managed_resource_addresses
    : module.managed_resource_address ? [module.managed_resource_address] : [];
  return addresses.length === 1
    && module.resource_types?.length === 1
    && addresses[0].split('.', 1)[0] === resource.terraform_type;
}

function updateResourceSelection(event) {
  if (!event.target.matches('[data-select-resource]') || !workflow) return;
  const selected = new Set(workflow.selected_resource_ids || []);
  if (event.target.checked) selected.add(event.target.dataset.selectResource);
  else selected.delete(event.target.dataset.selectResource);
  workflow.selected_resource_ids = workflow.ordered_resource_ids.filter(id => selected.has(id));
  $('resource-selection-count').textContent = `${workflow.selected_resource_ids.length} selected`;
  configureNextButton();
}

function selectSupportedResources() {
  if (!workflow || !['discovered', 'matched'].includes(workflow.stage)) return;
  const tracked = workflow.managed_resources || {};
  workflow.selected_resource_ids = workflow.ordered_resource_ids.filter(id => workflow.resources.find(resource => resource.id === id)?.support_status === 'supported' && !tracked[id]);
  renderWorkflowResources();
  configureNextButton();
}

function clearResourceSelection() {
  if (!workflow || !['discovered', 'matched'].includes(workflow.stage)) return;
  workflow.selected_resource_ids = [];
  renderWorkflowResources();
  configureNextButton();
}

function updateModuleSelection(event) {
  if (!event.target.matches('[data-module-resource]') || !workflow) return;
  workflow.module_selections ||= {};
  workflow.module_selections[event.target.dataset.moduleResource] = event.target.value;
  renderModuleMatches();
}

async function applyModuleSelectionPolicy(policy) {
  if (wizardBusy || workflow?.stage !== 'matched') return;
  wizardBusy = true;
  clearMessages();
  renderWizard();
  try {
    workflow = await runAction('select-modules', {policy});
    showSuccess(
      policy === 'latest-compatible'
        ? 'Latest compatible module versions selected in bulk. Resources without an exact single-resource module keep the direct-resource fallback.'
        : 'All selected infrastructure will use direct provider resources.'
    );
  } catch (error) { showError(error); }
  finally { wizardBusy = false; renderWizard(); }
}

async function searchPublicModules() {
  if (wizardBusy || workflow?.stage !== 'matched') return;
  wizardBusy = true;
  clearMessages();
  renderWizard();
  try {
    workflow = await runAction('match', {selected_resource_ids: workflow.selected_resource_ids, include_public: true});
    showSuccess('Public Terraform Registry search completed. Select a candidate only when it represents the existing resource without drift, or keep the direct-resource fallback.');
  } catch (error) { showError(error); }
  finally { wizardBusy = false; renderWizard(); }
}

async function returnToModuleMatching() {
  if (wizardBusy || workflow?.stage !== 'generated') return;
  wizardBusy = true;
  clearMessages();
  renderWizard();
  try {
    workflow = await runAction('match', {
      selected_resource_ids: workflow.selected_resource_ids,
      include_public: Boolean(workflow.public_registry_searched),
    });
    delete phaseJobLogs.generate;
    delete phaseJobLogs.validate;
    currentScreen = 2;
    showSuccess('Generated artifacts were discarded. Review module choices and compose again.');
  } catch (error) { showError(error); }
  finally {
    wizardBusy = false;
    renderWizard();
  }
}

function renderComposition() {
  $('terraform').textContent = terraformArtifactText(workflow.bundle);
  $('agent-meta').textContent = `${workflow.bundle.agent || 'AI agent'} · ${workflow.bundle.duration_ms || 0} ms`;
  $('composition-log').innerHTML = listItems(workflow.bundle.composition_log, 'No execution metadata returned.');
  $('prompt-evidence').textContent = workflow.bundle.prompt_sha256 || 'Not available';
  $('response-evidence').textContent = workflow.bundle.response_sha256 || 'Not available';
  $('redaction-count').textContent = workflow.bundle.redacted_fields || 0;
  $('assurance-checks').innerHTML = listItems(workflow.bundle.assurance_checks, 'No assurance checks were recorded.');
  $('composition-decisions').innerHTML = listItems(workflow.bundle.decisions, 'No module decisions returned.');
  $('composition-imports').innerHTML = workflow.bundle.imports.map(item => `<tr><td><code>${escapeHtml(item.address)}</code></td><td><code>${escapeHtml(item.remote_id)}</code></td></tr>`).join('');
  $('composition-warnings').classList.toggle('hidden', !workflow.bundle.warnings.length);
  $('composition-warnings').innerHTML = workflow.bundle.warnings.map(item => `<div>⚠ ${escapeHtml(item)}</div>`).join('');
}

function renderCompositionProgress() {
  const events = workflow?.events || [];
  const sessionStart = events.map(event => event.startsWith('AI composition queued')).lastIndexOf(true);
  const eventLog = (sessionStart >= 0 ? events.slice(sessionStart) : events).filter(event =>
    event.startsWith('AI composition') || event.startsWith('AI assurance')
  );
  const job = activeJob?.kind === 'adoption' && activeJob.action === 'generate' && activeJob.subject_id === workflow?.id
    ? activeJob
    : null;
  const sessionEvents = phaseJobLogs.generate?.length ? phaseJobLogs.generate : eventLog;
  const provider = settings?.ai_provider === 'github-copilot' ? 'GitHub Copilot' : 'IBM Bob';
  let status = 'Ready';
  let statusClass = '';
  let processLabel = `Waiting for ${provider}`;
  if (job && ['queued', 'running'].includes(job.status)) {
    status = job.status === 'queued' ? 'Queued' : 'Running';
    statusClass = 'running';
    processLabel = `${provider} · ${job.status} · attempt ${job.attempts || 0}/${job.max_attempts}`;
  } else if (job?.status === 'failed' || sessionEvents.at(-1)?.startsWith('AI assurance: final proposal rejected')) {
    status = 'Rejected';
    statusClass = 'failed';
    processLabel = `${provider} proposal rejected by assurance`;
  } else if (workflow?.bundle) {
    status = 'Accepted';
    statusClass = 'accepted';
    processLabel = `${provider} composition accepted`;
  } else if (sessionEvents.length) {
    status = 'Paused';
    processLabel = `${provider} session available for retry`;
  }
  const statusElement = $('composition-run-status');
  statusElement.textContent = status;
  statusElement.className = `agent-run-status ${statusClass}`.trim();
  $('composition-process-label').textContent = processLabel;
  $('composition-live-log').textContent = sessionEvents.length
    ? sessionEvents.map(event => `$ ${event}`).join('\n')
    : '$ Select representations and start composition to open an agent session.';
  const terminal = $('composition-live-log').parentElement;
  terminal.scrollTop = terminal.scrollHeight;
}

function captureJobLogs(job) {
  if (!job || job.kind !== 'adoption') return;
  const logs = Array.isArray(job.result?.logs) ? job.result.logs : [];
  if (logs.length) phaseJobLogs[job.action] = logs;
}

function renderJobTerminal(action, prefix, emptyMessage) {
  const job = activeJob?.kind === 'adoption' && activeJob.action === action && activeJob.subject_id === workflow?.id
    ? activeJob
    : null;
  const logs = job?.result?.logs || phaseJobLogs[action] || [];
  let status = logs.length ? 'Complete' : 'Ready';
  let statusClass = logs.length ? 'accepted' : '';
  let label = logs.length ? `${action} session retained` : `Waiting for ${action}`;
  if (job && ['queued', 'running'].includes(job.status)) {
    status = job.status === 'queued' ? 'Queued' : 'Running';
    statusClass = 'running';
    label = `${action} · ${job.status} · attempt ${job.attempts || 0}/${job.max_attempts}`;
  } else if (job?.status === 'failed') {
    status = 'Failed';
    statusClass = 'failed';
    label = `${action} failed · log retained`;
  }
  const statusElement = $(`${prefix}-run-status`);
  if (!statusElement) return;
  statusElement.textContent = status;
  statusElement.className = `agent-run-status ${statusClass}`.trim();
  $(`${prefix}-process-label`).textContent = label;
  $(`${prefix}-live-log`).textContent = logs.length
    ? logs.map(item => `$ ${item}`).join('\n')
    : `$ ${emptyMessage}`;
  const terminal = $(`${prefix}-live-log`).parentElement;
  terminal.scrollTop = terminal.scrollHeight;
}

function renderHcpTerminal() {
  const activeLogs = activeJob?.kind === 'adoption' && ['import', 'reconcile'].includes(activeJob.action)
    ? activeJob.result?.logs || []
    : [];
  const action = activeJob?.action === 'reconcile' ? 'reconcile' : 'import';
  if (activeLogs.length) phaseJobLogs[action] = activeLogs;
  const importLogs = phaseJobLogs.import || [];
  const reconcileLogs = phaseJobLogs.reconcile || [];
  const combined = [...importLogs, ...reconcileLogs];
  renderJobTerminal(action, 'hcp', 'Authorize submission to open the HCP Terraform session.');
  if (combined.length) {
    $('hcp-live-log').textContent = combined.map(item => `$ ${item}`).join('\n');
    const terminal = $('hcp-live-log').parentElement;
    terminal.scrollTop = terminal.scrollHeight;
  }
}

function renderVerification() {
  const result = workflow.verification;
  const accepted = Boolean(workflow.drift_accepted && result.drift_detected);
  const passed = result.success && !result.drift_detected;
  $('verification-badge').textContent = accepted ? 'Drift accepted' : passed ? 'Passed' : 'Blocked';
  $('verification-badge').className = `verification-badge ${accepted ? 'warn' : passed ? 'pass' : 'fail'}`;
  $('verification-summary').textContent = result.summary;
  const repairEvidence = result.repair_attempts
    ? `<div class="detail-item"><strong>${result.repaired_by_ai ? '✓ AI repair accepted' : '⚠ AI repair exhausted'}</strong><small>${escapeHtml(`${result.repair_attempts} bounded attempt${result.repair_attempts === 1 ? '' : 's'}; every proposal was rechecked by host assurance and Terraform.`)}</small></div>`
    : '';
  $('verification-diagnostics').innerHTML = repairEvidence + listItems(result.diagnostics, 'No Terraform diagnostics.');
  $('verification-commands').innerHTML = result.commands.map(command => `<code>$ ${escapeHtml(command)}</code>`).join('');
  $('verification-output').textContent = result.output;
  const driftPresent = Boolean(result.drift_detected);
  const completeImports = Boolean(workflow.bundle && result.planned_imports === workflow.bundle.imports.length);
  const acceptanceInput = $('accept-drift');
  const acceptanceKey = `${workflow.id}:${workflow.updated_at}:${result.planned_imports}:${workflow.bundle?.imports.length || 0}`;
  $('drift-acceptance').classList.toggle('hidden', !driftPresent);
  if (driftAcceptanceRenderKey !== acceptanceKey) {
    acceptanceInput.checked = accepted;
    driftAcceptanceRenderKey = acceptanceKey;
  } else if (accepted) {
    acceptanceInput.checked = true;
  }
  if (!driftPresent || !completeImports) acceptanceInput.checked = false;
  acceptanceInput.disabled = wizardBusy || accepted || !completeImports;
  $('drift-acceptance-note').textContent = completeImports
    ? 'Check this confirmation, then click “Accept drift and continue” below. The HCP saved plan may include create, update, or destroy actions in addition to imports and must be reviewed before apply.'
    : 'This drift cannot be accepted because the plan does not contain every expected import.';
  $('return-to-match').classList.toggle('hidden', result.success && !result.drift_detected);
  $('return-to-match').disabled = wizardBusy || workflow.stage !== 'generated';
}

function renderImportReview() {
  if (!workflow?.bundle || !['validated', 'submitted', 'imported'].includes(workflow.stage)) return;
  $('import-target').textContent = settings?.hcp_organization && settings?.hcp_workspace ? `${settings.hcp_organization} / ${settings.hcp_workspace}` : 'Not configured';
  $('import-operation-count').textContent = `${workflow.bundle.imports.length} resources`;
  $('import-plan-status').textContent = workflow.drift_accepted ? 'Drift accepted · manual review required' : 'Import-only · no drift';
  $('import-badge').textContent = workflow.stage === 'submitted' ? `Saved plan · ${workflow.import_run_id}` : workflow.stage === 'imported' ? `Completed · ${workflow.import_run_id}` : 'Ready for submission';
  $('import-boundary-title').textContent = 'Submit a provisional saved plan';
  $('import-boundary-copy').textContent = workflow.drift_accepted
    ? 'TerraMig uploads the configuration and queues a saved HCP plan containing explicitly accepted drift. Wait for the remote destroy gate to pass, then inspect every non-import action before applying it manually in HCP Terraform.'
    : 'TerraMig uploads the reviewed configuration and queues a saved HCP plan. It does not auto-apply. Wait for the remote destroy gate to pass, review the plan, then explicitly apply it in HCP Terraform to perform the imports.';
  $('adoption-confirmation').classList.toggle('hidden', workflow.stage !== 'validated');
  $('nonempty-workspace-override').classList.toggle('hidden', workflow.stage !== 'validated');
  const protection = workflow.target_protection;
  $('target-protection-evidence').innerHTML = protection?.checks?.length
    ? protection.checks.map(check => `<div>✓ ${escapeHtml(check)}</div>`).join('')
    : '<div>Protection runs twice: before upload and immediately before saved-plan submission.</div>';
  const lifecycle = workflow.hcp_import || {};
  const lifecycleItems = [
    ['Saved plan', lifecycle.run_id || 'Not submitted'],
    ['Remote status', lifecycle.remote_status || lifecycle.status || 'Not submitted'],
    ['Remote destroy gate', lifecycle.status === 'awaiting-hcp-apply' ? 'Passed · zero destroys' : lifecycle.status === 'destructive-plan-discarded' ? 'Blocked · run discarded' : lifecycle.status === 'remote-plan-check' ? 'Checking · do not apply' : 'Pending'],
    ['Post-import plan', lifecycle.post_import_run_id || 'Pending HCP apply'],
    ['Drift assurance', lifecycle.drift_free === true ? 'Zero drift confirmed' : lifecycle.drift_free === false ? 'Drift detected' : 'Pending'],
  ];
  const hcpLinks = [
    lifecycle.workspace_url ? `<a class="metrics-link" href="${escapeHtml(lifecycle.workspace_url)}" target="_blank" rel="noreferrer">Open HCP workspace ↗</a>` : '',
    lifecycle.run_url ? `<a class="metrics-link" href="${escapeHtml(lifecycle.run_url)}" target="_blank" rel="noreferrer">Open HCP run ↗</a>` : '',
  ].join('');
  $('hcp-lifecycle').innerHTML = lifecycleItems.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('') + hcpLinks;
  if (workflow.stage === 'submitted') monitorHcpImport(workflow.id);
}

function openWorkflowResource(event) {
  const target = event.target.closest('[data-open-resource]');
  if (!target || !workflow) return;
  const resource = workflow.resources.find(item => item.id === target.dataset.resourceId);
  openResourceDrawer(resource, workflow.candidates?.[resource.id] || []);
}

function openResourceDrawer(resource, candidates) {
  if (!resource) return;
  const management = workflow?.managed_resources?.[resource.id];
  $('drawer-title').textContent = resource.name;
  $('drawer-id').textContent = resource.id;
  $('drawer-facts').innerHTML = `<div><span>Cloud Asset type</span><strong>${escapeHtml(resource.type)}</strong></div><div><span>Location</span><strong>${escapeHtml(resource.location)}</strong></div><div><span>Migration domain</span><strong>${escapeHtml(resourceDomain(resource))}</strong></div><div><span>Management tracking</span><strong>${management ? 'Previously imported' : 'Not tracked'}</strong></div>`;
  const managementEvidence = management
    ? `<div class="detail-item"><strong>Completed TerraMig lifecycle</strong><small>${escapeHtml(management.target || 'HCP Terraform')} · ${escapeHtml(management.address || 'address unavailable')} · ${escapeHtml(management.verified_at || 'time unavailable')}</small>${management.run_url ? `<a class="metrics-link" href="${escapeHtml(management.run_url)}" target="_blank" rel="noreferrer">Open recorded HCP run ↗</a>` : ''}</div>`
    : '<div class="detail-item"><strong>No prior import evidence</strong><small>No completed, drift-free TerraMig lifecycle records this resource. This is not a live query across every HCP workspace.</small></div>';
  $('drawer-capability').innerHTML = `${managementEvidence}<div class="detail-item"><strong>${supportBadge(resource)} ${escapeHtml(resource.terraform_type || 'No Terraform mapping')}</strong><small>${escapeHtml(resource.support_reason || 'Capability has not been assessed.')}</small></div><div class="detail-item"><strong>Provider import ID</strong><small>${escapeHtml(resource.import_id || 'Not resolved')}</small></div><div class="detail-item"><strong>Capability catalog</strong><small>hashicorp/google ${escapeHtml(resource.provider_version || 'unversioned')}</small></div>`;
  $('drawer-attributes').innerHTML = Object.entries(resource.attributes).map(([key, value]) => `<div><span>${escapeHtml(key)}</span><code>${escapeHtml(typeof value === 'object' ? JSON.stringify(value, null, 2) : value)}</code></div>`).join('') || '<div class="empty">No attributes discovered.</div>';
  $('drawer-dependencies').innerHTML = listItems(resource.dependency_ids, 'No dependencies.');
  $('drawer-candidates').innerHTML = candidates.length ? candidates.map(candidate => `<div class="detail-item"><strong>${escapeHtml(candidate.module.source)} @ ${escapeHtml(candidate.module.version)}</strong><small>${Math.round(candidate.score * 100)}% match · ${escapeHtml(candidate.reasons.join(' · '))}</small></div>`).join('') : '<div class="empty">Module matching has not run, or no compatible module matched.</div>';
  $('resource-drawer').classList.remove('hidden');
  $('drawer-backdrop').classList.remove('hidden');
}

function supportBadge(resource) {
  const status = resource.support_status || 'unknown';
  const labels = {supported: 'Supported', partial: 'Partial', unsupported: 'Not importable', unknown: 'Unknown'};
  return `<span class="capability-badge ${escapeHtml(status)}">${labels[status] || escapeHtml(status)}</span>`;
}

function closeResourceDrawer() {
  $('resource-drawer').classList.add('hidden');
  $('drawer-backdrop').classList.add('hidden');
}

function listItems(items, empty) {
  return items?.length ? items.map(item => `<div class="detail-item">${escapeHtml(item)}</div>`).join('') : `<div class="empty">${escapeHtml(empty)}</div>`;
}

async function copyTerraform() {
  if (!workflow?.bundle) return;
  await navigator.clipboard.writeText(terraformArtifactText(workflow.bundle));
  $('copy').textContent = 'Copied';
  setTimeout(() => $('copy').textContent = 'Copy code', 1200);
}

function terraformArtifactText(bundle) {
  const imports = (bundle.imports || []).map(item =>
    `import {\n  to = ${item.address}\n  id = ${JSON.stringify(item.remote_id)}\n}`
  ).join('\n\n');
  return [
    ['backend.tf', bundle.backend_tf],
    ['providers.tf', bundle.providers_tf],
    ['main.tf', bundle.terraform],
    ['imports.tf', imports],
  ].filter(([, content]) => content?.trim())
    .map(([name, content]) => `# ${name}\n${content.trim()}\n`)
    .join('\n');
}

async function loadOperations() {
  setBusy($('operations-refresh'), true);
  try {
    const [data, workflowHistory, jobHistory] = await Promise.all([
      api('/api/operations'),
      api('/api/workflows'),
      api('/api/jobs'),
    ]);
    const failures = data.operations.filter(item => item.status === 'failed').reduce((total, item) => total + item.count, 0);
    $('operations-uptime').textContent = `${data.uptime_seconds}s`;
    $('operations-workflows').textContent = data.workflows.total;
    $('operations-failures').textContent = failures;
    const activeJobs = jobHistory.jobs.filter(job => ['queued', 'running'].includes(job.status)).length;
    $('operations-jobs').textContent = `${activeJobs} active · ${jobHistory.jobs.length} retained`;
    $('operations-updated').textContent = new Date().toLocaleTimeString();
    $('operations-body').innerHTML = data.recent.length ? data.recent.map(item => `<tr><td>${escapeHtml(new Date(item.timestamp).toLocaleTimeString())}</td><td><span class="tag">${escapeHtml(item.kind)}</span></td><td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.subject_id || item.request_id || '')}</small></td><td><span class="operation-status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span></td><td>${escapeHtml(item.duration_ms)} ms</td></tr>`).join('') : '<tr><td colspan="5" class="empty-cell">No operational events recorded yet.</td></tr>';
    $('jobs-body').innerHTML = jobHistory.jobs.length ? jobHistory.jobs.map(job => {
      const control = ['queued', 'running'].includes(job.status)
        ? `<button class="ghost compact" data-job-id="${escapeHtml(job.id)}" data-job-action="cancel">Cancel</button>`
        : ['failed', 'cancelled'].includes(job.status)
          ? `<button class="ghost compact" data-job-id="${escapeHtml(job.id)}" data-job-action="retry">Retry</button>`
          : '—';
      return `<tr><td><strong>${escapeHtml(job.id.slice(0, 8))}</strong><small>${escapeHtml(job.subject_id)}</small></td><td>${escapeHtml(job.kind)}.${escapeHtml(job.action)}</td><td><span class="operation-status ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>${job.last_error ? `<small>${escapeHtml(job.last_error)}</small>` : ''}</td><td>${job.attempts} / ${job.max_attempts}</td><td>${control}</td></tr>`;
    }).join('') : '<tr><td colspan="5" class="empty-cell">No durable jobs recorded yet.</td></tr>';
    $('persisted-workflows').innerHTML = workflowHistory.workflows.length
      ? workflowHistory.workflows.map(item => {
        const legacy = item.runtime_origin !== 'production';
        const status = legacy ? `${item.stage} · restart required` : item.stage;
        return `<button class="persisted-run" data-workflow-id="${escapeHtml(item.id)}"><span><strong>${escapeHtml(item.project_id)}</strong><small>${escapeHtml(item.updated_at ? new Date(item.updated_at).toLocaleString() : item.id)}</small></span><span class="tag">${escapeHtml(status)}</span></button>`;
      }).join('')
      : '<div class="empty">No persisted workflows.</div>';
  } catch (error) { showError(error); }
  finally { setBusy($('operations-refresh'), false); }
}

async function controlJob(event) {
  const button = event.target.closest('[data-job-id]');
  if (!button) return;
  try {
    await api(`/api/jobs/${button.dataset.jobId}/${button.dataset.jobAction}`, {method: 'POST', body: '{}'});
    await loadOperations();
  } catch (error) {
    showError(error);
  }
}

async function resumeWorkflow(event) {
  const button = event.target.closest('[data-workflow-id]');
  if (!button) return;
  try {
    workflow = await api(`/api/workflows/${button.dataset.workflowId}`);
    gitDeliveryFormWorkflowId = null;
    activeJob = null;
    phaseJobLogs = {};
    const jobs = await api('/api/jobs');
    jobs.jobs
      .filter(job => job.subject_id === workflow.id)
      .slice()
      .reverse()
      .forEach(captureJobLogs);
    resourceVisibility = 'supported';
    resourceManagementVisibility = 'unmanaged';
    resourceGrouping = 'domain';
    resourceSearch = '';
    matchGrouping = 'decision';
    matchSearch = '';
    expandedResourceGroups.clear();
    expandedMatchGroups.clear();
    currentScreen = maxAccessibleScreen();
    showView('workflow');
    renderWizard();
    showSuccess(`Restored adoption run ${workflow.id}.`);
  } catch (error) {
    showError(error);
  }
}

async function downloadReport() {
  if (!workflow) return;
  const report = await api(`/api/workflows/${workflow.id}/report`);
  const blob = new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `terramig-workflow-${workflow.id}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function resetWorkflow() {
  workflow = null;
  gitDeliveryFormWorkflowId = null;
  driftAcceptanceRenderKey = '';
  activeJob = null;
  phaseJobLogs = {};
  currentScreen = 0;
  resourceVisibility = 'supported';
  resourceManagementVisibility = 'unmanaged';
  resourceGrouping = 'domain';
  resourceSearch = '';
  matchGrouping = 'decision';
  matchSearch = '';
  expandedResourceGroups.clear();
  expandedMatchGroups.clear();
  $('adoption-confirm').checked = false;
  $('allow-nonempty-workspace').checked = false;
  $('accept-drift').checked = false;
  $('git-target-repository').value = '';
  $('git-delivery-branch').value = '';
  $('git-target-path').value = 'terraform';
  $('git-create-pr').checked = Boolean(settings?.readiness.github_api);
  $('git-update-existing-directory').checked = false;
  $('git-allow-overwrite').checked = false;
  renderWizard();
}

function resourceGlyph(type) {
  if (type.includes('Subnetwork')) return 'SUB';
  if (type.includes('Network')) return 'NET';
  if (type.includes('Instance')) return 'VM';
  return 'OBJ';
}
function showError(error) { $('error').textContent = error.message; $('error').classList.remove('hidden'); $('success').classList.add('hidden'); }
function showSuccess(message) { $('success').textContent = message; $('success').classList.remove('hidden'); $('error').classList.add('hidden'); }
function clearMessages() { $('error').classList.add('hidden'); $('success').classList.add('hidden'); }
function setBusy(button, busy) { button.disabled = busy; }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character])); }

boot();
