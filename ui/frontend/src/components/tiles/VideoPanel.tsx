interface VideoPanelProps {
  videoUrl?: string;
}

export function VideoPanel({ videoUrl }: VideoPanelProps) {
  return (
    <div className="tile" data-tile="video">
      <div className="tile__header">
        <h3>Raw Video</h3>
        <span className="tile__badge tile__badge--real">SAR-RARP50</span>
      </div>
      <div className="tile__body tile__body--center">
        {videoUrl ? (
          <video src={videoUrl} controls className="tile__video" />
        ) : (
          <p className="tile__placeholder">No case video loaded — waiting for a case to be opened.</p>
        )}
      </div>
    </div>
  );
}
