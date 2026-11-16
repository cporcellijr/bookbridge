# Read-aloud EPUB cover extraction

Cover extraction reads only `META-INF/container.xml`, the referenced OPF, and
the selected image. It does not load the book through ebooklib, which eagerly
reads manifest entries, including potentially gigabytes of embedded narration.

EPUB 3 `cover-image` properties and EPUB 2 `meta name="cover"` references take
precedence over the existing cover-filename heuristic. OPF-relative and
percent-encoded image paths are supported. Metadata is limited to 2 MiB per
entry and cover images to 32 MiB; invalid or oversized inputs fail without
replacing an existing cover. Images are published with an atomic rename.

A fixed pool of request locks makes concurrent requests for the same missing
cover wait for one extraction instead of doing duplicate work (or returning a
temporary missing-cover response).

## Verification

Regression tests guard ZIP entry access so attempts to read audio, chapter
content, or use the full-book parser fail. They also cover cover selection,
failure cleanup, atomic publication, and concurrent lazy requests.

The equivalent cover-only hotfix was tested on a deployed 7.13.0 instance with a
1.71 GB read-aloud EPUB: four concurrent authenticated cold-cache HTTP requests
returned the same 418,474-byte cover with status 200 in 0.24 seconds total.
Observed cgroup memory growth during the check was approximately 10 MiB (not a
process-peak measurement; other background jobs were running).

On upstream `dev`, the ebook, cover, and Readest-upload regression group passed
on that same host: **300 passed, 3 skipped, 8 subtests passed**.
