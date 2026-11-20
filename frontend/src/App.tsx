import IngestionPanel, { type Project } from './IngestionPanel';
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { ArrowRight, Check, ChevronRight, Film, FolderOpen, LayoutDashboard, Layers3, LoaderCircle, LockKeyhole, Link2, MonitorPlay, Upload, Plus, Scissors, Sparkles, Subtitles, Trash2, WandSparkles, X } from 'lucide-react';

type Connection = 'checking' | 'connected' | 'offline';

const steps = [
  { icon: Link2, MonitorPlay, Upload, number: '01', title: 'Add your video', text: 'Paste a YouTube link or upload a video from your computer.' },
  { icon: WandSparkles, number: '02', title: 'Find the highlights', text: 'AI will find the moments worth sharing from your video.' },
  { icon: Subtitles, number: '03', title: 'Make it yours', text: 'Fine-tune your clips and captions before downloading.' },
];

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' }).format(date);
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [connection, setConnection] = useState<Connection>('checking');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [selectedProject, setSelectedProject] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [health, list] = await Promise.all([fetch('/api/health'), fetch('/api/projects')]);
      if (!health.ok || !list.ok) throw new Error('API unavailable');
      setProjects(await list.json());
      setConnection('connected');
    } catch {
      setConnection('offline');
      setError('Could not connect to the local backend. Make sure it is running, then try again.');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function createProject(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || saving) return;
    setSaving(true); setError('');
    try {
      const response = await fetch('/api/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name.trim() }) });
      if (!response.ok) throw new Error('Create failed');
      const project: Project = await response.json();
      setProjects(current => [project, ...current]);
      setName(''); setShowForm(false); setConnection('connected'); setSelectedProject(project.id);
    } catch { setError('Could not create the project. Check that the local backend is running and try again.'); }
    finally { setSaving(false); }
  }

  async function deleteProject(project: Project) {
    if (!window.confirm(`Delete “${project.name}” and its associated video files? This cannot be undone.`)) return;
    setDeleting(project.id); setError('');
    try {
      const response = await fetch(`/api/projects/${project.id}`, { method: 'DELETE' });
      if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || 'Could not delete project.'); }
      setProjects(current => current.filter(item => item.id !== project.id));
      if (selectedProject === project.id) setSelectedProject(null);
    } catch (error) { setError(error instanceof Error ? error.message : 'Could not delete the project. Please try again.'); }
    finally { setDeleting(null); }
  }

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Scissors size={20} strokeWidth={2.5} /></span><span>clips<span className="brand-light">studio</span><small>YOUR CREATIVE SPACE</small></span></div>
      <div className="sidebar-section"><div className="nav-label">WORKSPACE</div><div className="nav-active"><LayoutDashboard size={18} /><span>Dashboard</span><span className="nav-dot" /></div></div>
      <div className="sidebar-note"><div className="note-icon"><Sparkles size={17} /></div><strong>A little more magic,<br />coming soon.</strong><p>We're building your personal clipping workspace, one step at a time.</p><span>PHASE 02 <ArrowRight size={13} /></span></div>
      <div className="sidebar-bottom"><span className="privacy-icon"><LockKeyhole size={15} /></span><div><strong>Made for you, locally</strong><small>Your workspace stays yours.</small></div></div>
    </aside>

    <main className="main">
      <header className="topbar"><div className="breadcrumb">Workspace <ChevronRight size={14} /> <strong>Dashboard</strong></div><div className="top-right"><span className={`connection ${connection}`}><span className="status-dot" />{connection === 'connected' ? 'Local workspace' : connection === 'checking' ? 'Connecting' : 'Backend offline'}</span><span className="avatar">Y</span></div></header>
      <div className="content">
        <div className="welcome"><div><div className="eyebrow"><span /> YOUR CREATIVE WORKSPACE</div><h1>Big moments, <span>little clips.</span></h1><p>A little space for your next big idea. Let's make something worth sharing.</p></div><div className="welcome-decoration"><Sparkles size={21} /></div></div>

        {error && <div className="error" role="alert"><span>{error}</span><button type="button" onClick={() => void load()}>Try again</button></div>}

        <section className="hero-card">
          <div className="hero-copy"><div className="hero-tag"><Sparkles size={13} /> YOUR WORKSPACE IS READY</div><h2>From long videos<br />to <em>can't-miss</em> moments.</h2><p>Start a project today. Import a video into your project. AI-powered clip generation is coming later.</p><button className="primary-btn" onClick={() => setShowForm(true)} disabled={connection !== 'connected'}><Plus size={18} /> New project</button><div className="hero-foot"><LockKeyhole size={13} /> Private by design. Cloud features are optional.</div></div>
          <div className="hero-art" aria-hidden="true"><div className="art-orbit orbit-one" /><div className="art-orbit orbit-two" /><div className="art-star star-one">✳</div><div className="art-star star-two">✦</div><div className="art-video"><div className="video-top"><span /><span /><span /></div><div className="video-screen"><div className="play-triangle" /></div><div className="video-lines"><i /><i /></div></div><div className="art-clip"><div className="clip-screen"><span className="clip-play">▶</span><div className="caption-line" /><div className="caption-line short" /></div><div className="clip-bottom"><i /><i /></div></div><div className="art-scissors"><Scissors size={19} /></div><div className="art-spark"><Sparkles size={20} /></div></div>
        </section>

        <IngestionPanel projects={projects} selected={selectedProject} onSelect={setSelectedProject} online={connection === 'connected'} />

        <section className="projects-section"><div className="section-heading"><div><div className="section-kicker">YOUR LIBRARY</div><h2>Recent projects <span className="count">{projects.length}</span></h2><p>All your ideas, right where you left them.</p></div><button className="secondary-btn" onClick={() => setShowForm(true)} disabled={connection !== 'connected'}><Plus size={16} /> New project</button></div>
          {loading ? <div className="empty-state"><LoaderCircle className="spin" size={24} /><p>Loading your projects...</p></div> : projects.length === 0 ? <div className="empty-state"><div className="empty-icon"><FolderOpen size={25} strokeWidth={1.6} /><span><Plus size={12} /></span></div><h3>No projects just yet</h3><p>Your next great clip starts here. Create a project<br className="desktop-break" /> to keep everything organized.</p><button onClick={() => setShowForm(true)} disabled={connection !== 'connected'}>Create your first project <ArrowRight size={15} /></button></div> : <div className="project-list">{projects.map(project => <div className="project-row" key={project.id}><div className="project-icon"><Film size={20} /></div><button className="project-info project-open" onClick={() => { setSelectedProject(project.id); document.getElementById('source-title')?.scrollIntoView({ behavior: 'smooth' }); }}><strong>{project.name}</strong><span>Created {formatDate(project.created_at)} · Open project</span></button><span className="draft-badge">Project</span><button className="delete-btn" aria-label={`Delete ${project.name}`} title="Delete project" disabled={deleting !== null} onClick={() => void deleteProject(project)}><Trash2 size={17} /></button></div>)}</div>}
        </section>

        <section className="how-section"><div className="section-heading"><div><div className="section-kicker">THE BIG PICTURE</div><h2>Made for the moments</h2><p>Here's what's coming to your workspace.</p></div><span className="coming-label">COMING IN FUTURE PHASES</span></div><div className="steps-grid">{steps.map(({ icon: Icon, number, title, text }) => <div className="step-card" key={number}><div className="step-top"><div className="step-icon"><Icon size={19} /></div><span>{number}</span></div><h3>{title}</h3><p>{text}</p></div>)}</div></section>
        <footer><span>Made for creators, by a creator. <span className="heart">♥</span></span><span><Check size={14} /> Your projects are stored locally</span></footer>
      </div>
    </main>
    {showForm && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !saving) setShowForm(false); }}><div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div className="modal-header"><div className="modal-icon"><Layers3 size={20} /></div><button aria-label="Close" onClick={() => setShowForm(false)} disabled={saving}><X size={19} /></button></div><h2 id="modal-title">Start something new</h2><p>Give your project a name. You can import a video after creating your project.</p>{error && <div className="error" role="alert">{error}</div>}<form onSubmit={createProject}><label htmlFor="project-name">Project name</label><input id="project-name" value={name} onChange={event => setName(event.target.value)} placeholder="e.g. My podcast highlights" maxLength={120} autoFocus required /><div className="modal-actions"><button type="button" className="secondary-btn" onClick={() => setShowForm(false)} disabled={saving}>Cancel</button><button className="primary-btn" disabled={!name.trim() || saving}>{saving ? 'Creating...' : 'Create project'} <ArrowRight size={16} /></button></div></form></div></div>}
  </div>;
}
