package state

import "time"

const CurrentVersion = 4

type FileState string

const (
	FileObserved      FileState = "observed"
	FileSpooled       FileState = "spooled"
	FileUploading     FileState = "uploading"
	FileRetry         FileState = "retry"
	FileFailed        FileState = "failed"
	FileCancelPending FileState = "cancel_pending"
	FileCanceled      FileState = "canceled"
	FileReleased      FileState = "released"
	FileDurable       FileState = "durable"
)

type Observation struct {
	SourcePath   string    `json:"source_path"`
	RootName     string    `json:"root_name"`
	RelativePath string    `json:"relative_path"`
	Size         int64     `json:"size"`
	ModTimeNS    int64     `json:"mtime_ns"`
	StableCount  int       `json:"stable_count"`
	StableSince  time.Time `json:"stable_since"`
	LastSeenAt   time.Time `json:"last_seen_at"`
}

type File struct {
	ID                 string    `json:"id"`
	SourcePath         string    `json:"source_path"`
	SpoolPath          string    `json:"spool_path"`
	RootName           string    `json:"root_name"`
	RelativePath       string    `json:"relative_path"`
	RouteName          string    `json:"route_name,omitempty"`
	SegmentNumber      *int      `json:"segment_number,omitempty"`
	ArtifactType       string    `json:"artifact_type"`
	Camera             string    `json:"camera,omitempty"`
	Size               int64     `json:"size"`
	ModTimeNS          int64     `json:"mtime_ns"`
	SHA256             string    `json:"sha256"`
	CompletionEvidence []string  `json:"completion_evidence,omitempty"`
	Partial            bool      `json:"partial"`
	State              FileState `json:"state"`
	UploadID           string    `json:"upload_id,omitempty"`
	UploadOffset       int64     `json:"upload_offset"`
	UploadAttempt      int       `json:"upload_attempt"`
	AutoRedeclarations int       `json:"automatic_redeclarations"`
	Attempts           int       `json:"attempts"`
	NextAttemptAt      time.Time `json:"next_attempt_at,omitempty"`
	LastError          string    `json:"last_error,omitempty"`
	FirstObserved      time.Time `json:"first_observed_at"`
	SpooledAt          time.Time `json:"spooled_at,omitempty"`
	DurableAt          time.Time `json:"durable_at,omitempty"`
	ReleasedAt         time.Time `json:"released_at,omitempty"`
	CancelRequestedAt  time.Time `json:"cancel_requested_at,omitempty"`
	CancelNextState    FileState `json:"cancel_next_state,omitempty"`
	CancelDeleteRecord bool      `json:"cancel_delete_record,omitempty"`
	NeedsRespool       bool      `json:"needs_respool,omitempty"`
}

type CommandRecord struct {
	ID              string    `json:"id"`
	Type            string    `json:"type"`
	Fingerprint     string    `json:"fingerprint,omitempty"`
	State           string    `json:"state"`
	Message         string    `json:"message,omitempty"`
	Error           string    `json:"error,omitempty"`
	Action          string    `json:"action,omitempty"`
	ActionReason    string    `json:"action_reason,omitempty"`
	ActionQueuedAt  time.Time `json:"action_queued_at,omitempty"`
	ActionInvokedAt time.Time `json:"action_invoked_at,omitempty"`
	StartedAt       time.Time `json:"started_at"`
	FinishedAt      time.Time `json:"finished_at"`
	Reported        bool      `json:"reported"`
}

type UploadCancellation struct {
	FileID        string    `json:"file_id"`
	UploadID      string    `json:"upload_id"`
	RequestedAt   time.Time `json:"requested_at"`
	Attempts      int       `json:"attempts"`
	NextAttemptAt time.Time `json:"next_attempt_at,omitempty"`
	LastError     string    `json:"last_error,omitempty"`
}

type InventoryFile struct {
	ArtifactType string  `json:"artifact_type"`
	Camera       *string `json:"camera"`
	MTimeNS      int64   `json:"mtime_ns"`
	RelativePath string  `json:"relative_path"`
	SHA256       string  `json:"sha256"`
	Size         int64   `json:"size"`
}

type ExpectedStream struct {
	ArtifactType string  `json:"artifact_type"`
	Camera       *string `json:"camera"`
	Role         string  `json:"role"`
	RootName     string  `json:"root_name"`
}

type InventoryStream struct {
	MTimeNS      *int64  `json:"mtime_ns"`
	RelativePath *string `json:"relative_path"`
	Role         string  `json:"role"`
	SHA256       *string `json:"sha256"`
	Size         *int64  `json:"size"`
	Status       string  `json:"status"`
}

type InventorySegment struct {
	Files   []InventoryFile   `json:"files"`
	Number  int               `json:"number"`
	Streams []InventoryStream `json:"streams"`
}

type RouteManifest struct {
	CapabilitySource       string             `json:"capability_source"`
	ClosedAt               time.Time          `json:"closed_at"`
	ClosureEvidence        []string           `json:"closure_evidence"`
	ExpectedStreams        []ExpectedStream   `json:"expected_streams"`
	Generation             int                `json:"generation"`
	MissingSegmentNumbers  []int              `json:"missing_segment_numbers"`
	PreviousManifestSHA256 *string            `json:"previous_manifest_sha256"`
	RootNames              []string           `json:"root_names"`
	RouteClosed            bool               `json:"route_closed"`
	RouteFiles             []InventoryFile    `json:"route_files"`
	RouteName              string             `json:"route_name"`
	Schema                 string             `json:"schema"`
	SchemaVersion          int                `json:"schema_version"`
	Segments               []InventorySegment `json:"segments"`
	State                  string             `json:"state"`
}

type RouteInventory struct {
	ContentSHA256  string        `json:"content_sha256"`
	Manifest       RouteManifest `json:"manifest"`
	ManifestSHA256 string        `json:"manifest_sha256"`
	Attempts       int           `json:"attempts"`
	NextAttemptAt  time.Time     `json:"next_attempt_at,omitempty"`
	LastError      string        `json:"last_error,omitempty"`
	CapturedAt     time.Time     `json:"captured_at"`
	DeclaredAt     time.Time     `json:"declared_at,omitempty"`
}

type Counters struct {
	BytesUploaded        int64 `json:"bytes_uploaded"`
	FilesDurable         int64 `json:"files_durable"`
	ScanErrors           int64 `json:"scan_errors"`
	UploadErrors         int64 `json:"upload_errors"`
	CancelErrors         int64 `json:"cancel_errors"`
	FilesReleased        int64 `json:"files_released"`
	OrphansQuarantined   int64 `json:"orphans_quarantined"`
	SpoolCleanupErrors   int64 `json:"spool_cleanup_errors"`
	SpoolFilesReconciled int64 `json:"spool_files_reconciled"`
	FilesCompacted       int64 `json:"files_compacted"`
	InventoriesCaptured  int64 `json:"inventories_captured"`
	InventoriesDeclared  int64 `json:"inventories_declared"`
	InventoryErrors      int64 `json:"inventory_errors"`
}

type Journal struct {
	Version       int                           `json:"version"`
	Paused        bool                          `json:"paused"`
	Observations  map[string]Observation        `json:"observations"`
	Files         map[string]File               `json:"files"`
	Cancellations map[string]UploadCancellation `json:"upload_cancellations"`
	Inventories   map[string]RouteInventory     `json:"route_inventories"`
	Commands      map[string]CommandRecord      `json:"commands"`
	Counters      Counters                      `json:"counters"`
	LastScanAt    time.Time                     `json:"last_scan_at,omitempty"`
}

func EmptyJournal() Journal {
	return Journal{
		Version:       CurrentVersion,
		Observations:  make(map[string]Observation),
		Files:         make(map[string]File),
		Cancellations: make(map[string]UploadCancellation),
		Inventories:   make(map[string]RouteInventory),
		Commands:      make(map[string]CommandRecord),
	}
}

func (j *Journal) Normalize() {
	if j.Version == 0 {
		j.Version = CurrentVersion
	}
	if j.Observations == nil {
		j.Observations = make(map[string]Observation)
	}
	if j.Files == nil {
		j.Files = make(map[string]File)
	}
	if j.Cancellations == nil {
		j.Cancellations = make(map[string]UploadCancellation)
	}
	if j.Inventories == nil {
		j.Inventories = make(map[string]RouteInventory)
	}
	if j.Commands == nil {
		j.Commands = make(map[string]CommandRecord)
	}
}

func CancellationKey(fileID, uploadID string) string {
	return fileID + "\x00" + uploadID
}
