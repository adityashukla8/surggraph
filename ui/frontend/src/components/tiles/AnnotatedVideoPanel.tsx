interface AnnotatedVideoPanelProps {
  videoUrl?: string;
}

export function AnnotatedVideoPanel({ videoUrl }: AnnotatedVideoPanelProps) {
  return (
    <div className="tile" data-tile="annotated-video">
      <div className="tile__header">
        <h3>Segmented / Annotated Video</h3>
        <span className="tile__badge tile__badge--real">SAR-RARP50 ground truth</span>
      </div>
      <div className="tile__body tile__body--center">
        {videoUrl ? (
          <video src={videoUrl} controls className="tile__video" />
        ) : (
          <p className="tile__placeholder">
            No annotation overlay loaded — overlay format (masks vs. label strip) is confirmed once the dataset is
            downloaded.
          </p>
        )}
      </div>
    </div>
  );
}
