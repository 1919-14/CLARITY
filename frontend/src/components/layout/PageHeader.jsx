export function PageHeader({ volume, title, description, actions, lede = false, children }) {
  return (
    <header className="page-header">
      <div className="page-header__text">
        {volume ? <p className="volume-label">{volume}</p> : null}
        <h1 className="page-title">{title}</h1>
        {description ? (
          <p className={lede ? 'lede lede--dropcap' : 'lede'}>{description}</p>
        ) : null}
        {children}
      </div>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </header>
  );
}

export function OrnamentDivider({ glyph = '✶' }) {
  return (
    <div className="ornate-divider" role="presentation" aria-hidden="true">
      <span className="ornate-divider__glyph">{glyph}</span>
    </div>
  );
}
