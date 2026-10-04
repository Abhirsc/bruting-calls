# Bruting Calls — Shiny for R front-end.
# Single script. BirdNET processing, metadata reading and the PDF report run through the
# Python CLI in app.py (same folder), so this app and app.py give identical results.
#   BRUTING_PYTHON=/path/to/venv/bin/python Rscript -e "shiny::runApp('app.R')"

suppressPackageStartupMessages({
  library(shiny); library(bslib); library(ggplot2); library(dplyr)
  library(DT); library(jsonlite); library(processx); library(htmltools)
})

# ------------------------------------------------------------------ config
APP_PY      <- normalizePath("app.py", mustWork = TRUE)
PYTHON      <- Sys.getenv("BRUTING_PYTHON", "python3")
CACHE_ROOT  <- normalizePath(Sys.getenv("BRUTING_CACHE", "cache"), mustWork = FALSE)
CACHE_LIMIT <- as.numeric(Sys.getenv("BRUTING_CACHE_GB", "20")) * 1024^3
ALLOW_STOP  <- Sys.getenv("BRUTING_ALLOW_STOP") == "1"   # local use only: lets the browser stop the server
ORPHAN_TTL  <- as.numeric(Sys.getenv("BRUTING_ORPHAN_HOURS", "2")) * 3600
DEFAULT_TZ  <- Sys.getenv("BRUTING_TZ", "Australia/Sydney")
AUDIO_EXT   <- c("wav", "flac", "ogg")
dir.create(CACHE_ROOT, recursive = TRUE, showWarnings = FALSE)
Sys.setenv(BRUTING_CACHE = CACHE_ROOT)          # the worker shares processing slots through this
options(shiny.maxRequestSize = CACHE_LIMIT)
addResourcePath("ws", CACHE_ROOT)
dir.create("www", showWarnings = FALSE)
addResourcePath("static", normalizePath("www"))

# One teal hue (the King Parrot's wing) on warm cream, matching the 70s theme
BLUE <- "#2f7469"; BLUE_D <- "#1f4f47"; BLUE_L <- "#a9d3c9"; TEAL_MID <- "#6fb0a2"; INK <- "#3b2a1e"; INK2 <- "#6e5a48"
GRID <- "#eadbc4"; NEUTRAL <- "#f3e8d6"; SURF <- "#fffbf3"
SEQ <- c("#fffbf3", "#e4f1ec", "#c3e0d7", "#9ccbbf", "#6fb0a2", "#428f81", "#2f7469", "#1c4a42")

py <- function(...) processx::run(PYTHON, c(APP_PY, ...), error_on_status = TRUE, stderr_to_stdout = TRUE)

# App metadata (single source in app.py; shown on the About tab)
ABOUT <- fromJSON(py("about")$stdout)
APP_TITLE <- ABOUT$title; APP_VERSION <- ABOUT$version

# BirdNET species labels ("Common (Scientific)") for relabelling and IDs
LABELS <- tryCatch(fromJSON(py("labels")$stdout), error = function(e) character(0))
LABEL_SCI <- sub("^.*\\((.*)\\)$", "\\1", LABELS)
LABEL_COMMON <- sub("^(.*) \\(.*\\)$", "\\1", LABELS)
species_id <- function(sci) { i <- match(sci, LABEL_SCI); ifelse(is.na(i), "BN----", sprintf("BN%04d", i - 1)) }
sci_code <- function(sci) {
  p <- strsplit(gsub("[^A-Za-z ]", "", sci), " +")
  vapply(p, function(x) if (length(x) >= 2) toupper(paste0(substr(x[1], 1, 3), substr(x[2], 1, 3))) else toupper(substr(x[1], 1, 6)), "")
}
parse_label <- function(txt) {
  txt <- trimws(txt)
  i <- match(txt, LABELS)
  if (is.na(i)) i <- match(txt, LABEL_SCI)
  if (is.na(i)) i <- match(tolower(txt), tolower(LABEL_COMMON))
  if (is.na(i)) NULL else list(sci = LABEL_SCI[i], common = LABEL_COMMON[i])
}

# ------------------------------------------------------------------ helpers
`%||%` <- function(a, b) if (is.null(a) || length(a) == 0) b else a
fmt_bytes <- function(n) if (n < 1024^2) sprintf("%.0f KB", n / 1024) else if (n < 1024^3) sprintf("%.1f MB", n / 1024^2) else sprintf("%.2f GB", n / 1024^3)
fmt_secs <- function(s) if (s < 60) sprintf("%.0f s", s) else if (s < 3600) sprintf("%d min %02d s", s %/% 60, round(s %% 60)) else sprintf("%d h %02d min", s %/% 3600, (s %% 3600) %/% 60)
dir_size <- function(p) { f <- list.files(p, recursive = TRUE, all.files = TRUE, full.names = TRUE); sum(file.size(f), na.rm = TRUE) }
mtime <- function(p) { t <- file.mtime(p); if (is.na(t)) 0 else as.numeric(t) }
read_json_safe <- function(p) tryCatch(fromJSON(p, simplifyDataFrame = TRUE), error = function(e) NULL)
now_iso <- function() format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC")

sweep_cache <- function(keep) {
  for (d in list.dirs(CACHE_ROOT, recursive = FALSE)) {
    if (startsWith(basename(d), ".") || basename(d) == keep) next
    if (as.numeric(Sys.time()) - mtime(file.path(d, ".alive")) > ORPHAN_TTL) unlink(d, recursive = TRUE)
  }
}

DET_EMPTY <- data.frame(det_id = character(), file = character(), start_s = numeric(), end_s = numeric(),
                        datetime = as.POSIXct(character(), tz = "UTC"), date = character(), species_id = character(),
                        sci_code = character(), scientific = character(), common = character(), confidence = numeric(),
                        peak_hz = numeric(), low_hz = numeric(), high_hz = numeric(), snr_db = numeric(),
                        site = character(), lat = numeric(), lon = numeric(), location_source = character(), device = character())
load_det <- function(ws) {
  p <- file.path(ws, "job", "detections.csv")
  if (!file.exists(p) || file.size(p) == 0) return(DET_EMPTY)
  d <- read.csv(p, stringsAsFactors = FALSE)
  if (!nrow(d)) return(DET_EMPTY)
  d$datetime <- as.POSIXct(d$datetime, format = "%Y-%m-%d %H:%M:%S", tz = "UTC")
  d$date <- as.character(d$date)
  for (c in setdiff(names(DET_EMPTY), names(d))) d[[c]] <- NA
  d$site[is.na(d$site)] <- "site"
  d
}
load_files <- function(ws) {
  p <- file.path(ws, "job", "files.csv")
  if (!file.exists(p)) return(data.frame(file = character(), status = character(), start_local = as.POSIXct(character()), duration_s = numeric()))
  f <- read.csv(p, stringsAsFactors = FALSE)
  f$start_local <- as.POSIXct(f$start_local, format = "%Y-%m-%d %H:%M:%OS", tz = "UTC")
  f
}
VER_COLS <- c("det_id", "decision", "new_scientific", "new_common", "verifier", "note", "timestamp_utc",
              "scientific", "common", "confidence", "file", "start_s", "audio_sha")   # context feeds the site profile
load_ver <- function(ws) {
  p <- file.path(ws, "verifications.csv")
  if (!file.exists(p)) return(setNames(data.frame(matrix(character(), 0, length(VER_COLS))), VER_COLS))
  v <- read.csv(p, stringsAsFactors = FALSE, colClasses = "character")
  for (c in setdiff(VER_COLS, names(v))) v[[c]] <- ""
  v[is.na(v)] <- ""
  v <- v[order(v$timestamp_utc, method = "radix"), ]
  v <- v[!duplicated(v$det_id, fromLast = TRUE), ]
  v[v$decision != "clear", VER_COLS]
}
append_ver <- function(ws, row) {
  p <- file.path(ws, "verifications.csv")
  write.table(as.data.frame(row)[VER_COLS], p, sep = ",", row.names = FALSE, col.names = !file.exists(p),
              append = file.exists(p), qmethod = "double")
}

# ------------------------------------------------------------------ analysis (mirrors app.py)
with_decisions <- function(det, ver) {
  d <- left_join(det, ver[, c("det_id", "decision", "new_scientific", "new_common", "verifier")], by = "det_id")
  d$decision[is.na(d$decision) | d$decision == ""] <- "pending"
  d$original_common <- d$common
  d$original_scientific <- d$scientific
  rl <- d$decision == "relabel"
  if (any(rl)) {
    d$scientific[rl] <- d$new_scientific[rl]; d$common[rl] <- d$new_common[rl]
    d$species_id[rl] <- species_id(d$scientific[rl]); d$sci_code[rl] <- sci_code(d$scientific[rl])
  }
  d
}
filter_det <- function(det, ver, s) {
  d <- with_decisions(det, ver)
  d <- d[d$decision != "reject", ]
  verified <- d$decision %in% c("confirm", "relabel")
  thr <- rep(s$thr, nrow(d))
  per_sp <- !isFALSE(s$per_species)
  if (per_sp && length(s$thresholds)) {             # site-profile rules apply to BirdNET's own label
    m <- unlist(s$thresholds)[d$original_scientific]
    thr <- ifelse(is.na(m), thr, m)
  }
  keep <- d$confidence >= thr
  if (per_sp && length(s$blocked)) keep <- keep & !(d$original_scientific %in% unlist(s$blocked))
  if (isTRUE(s$override)) keep <- keep | verified
  if (isTRUE(s$verified_only)) keep <- keep & verified
  d <- d[keep, ]
  if (length(s$species)) d <- d[d$common %in% s$species, ]
  d[order(d$datetime), ]
}
call_events <- function(d, gap = 0) {
  if (!nrow(d)) return(data.frame(common = character(), species_id = character(), file = character(), site = character(),
                                  start_dt = as.POSIXct(character()), end_dt = as.POSIXct(character()),
                                  start_s = numeric(), end_s = numeric(), duration_s = numeric(), windows = integer(),
                                  max_conf = numeric(), gap_to_next_s = numeric(), verified = logical()))
  d %>% arrange(file, common, start_s) %>% group_by(file, common) %>%
    mutate(prev_end = lag(cummax(end_s)), ev = cumsum(is.na(prev_end) | start_s > prev_end + gap)) %>%
    group_by(file, common, ev) %>%
    summarise(scientific = first(scientific), species_id = first(species_id), dt0 = first(datetime - start_s),
              site = first(site), lat = first(lat), lon = first(lon),
              start_s = min(start_s), end_s = max(end_s), windows = n(), max_conf = max(confidence),
              verified = any(decision %in% c("confirm", "relabel")), .groups = "drop") %>%
    mutate(start_dt = dt0 + start_s, end_dt = dt0 + end_s, duration_s = end_s - start_s) %>%
    group_by(common, file) %>% arrange(start_s, .by_group = TRUE) %>%
    mutate(gap_to_next_s = round(lead(start_s) - end_s, 1)) %>% ungroup() %>% arrange(start_dt) %>%
    select(common, scientific, species_id, file, site, lat, lon, start_dt, end_dt, start_s, end_s, duration_s, windows, max_conf, gap_to_next_s, verified) %>%
    as.data.frame()
}
species_summary <- function(d, ev, det_all, ver, hours) {
  if (!nrow(d)) return(data.frame())
  dec <- with_decisions(det_all, ver)
  rej <- dec %>% filter(decision == "reject") %>% count(original_common, name = "rejected")
  e <- ev %>% group_by(common) %>% summarise(call_events = n(), total_call_s = round(sum(duration_s), 1),
                                              mean_call_s = round(mean(duration_s), 1),
                                              median_gap_s = suppressWarnings(median(gap_to_next_s, na.rm = TRUE)),
                                              min_gap_s = suppressWarnings(min(gap_to_next_s, na.rm = TRUE)),
                                              verified_events = sum(verified), .groups = "drop")
  d %>% group_by(common) %>%
    summarise(species_id = first(species_id), sci_code = first(sci_code), scientific = first(scientific),
              detections = n(), peak_hz = round(median(peak_hz, na.rm = TRUE)), low_hz = round(median(low_hz, na.rm = TRUE)),
              high_hz = round(median(high_hz, na.rm = TRUE)), first_heard = min(datetime), last_heard = max(datetime),
              days_heard = n_distinct(date), sites = n_distinct(site), max_conf = round(max(confidence), 3), mean_conf = round(mean(confidence), 3),
              confirmed = sum(decision %in% c("confirm", "relabel")), .groups = "drop") %>%
    left_join(e, by = "common") %>% left_join(rej, by = c("common" = "original_common")) %>%
    mutate(rejected = coalesce(rejected, 0L), calls_per_hour = if (hours > 0) round(call_events / hours, 2) else NA,
           min_gap_s = ifelse(is.infinite(min_gap_s), NA, min_gap_s)) %>%
    select(species_id, sci_code, common, scientific, detections, call_events, calls_per_hour, total_call_s, mean_call_s,
           median_gap_s, min_gap_s, peak_hz, low_hz, high_hz, first_heard, last_heard, days_heard, sites, max_conf, mean_conf,
           confirmed, rejected, verified_events) %>%
    arrange(desc(call_events), desc(detections)) %>% as.data.frame()
}
rejected_table <- function(det_all, ver, d) {
  bad <- with_decisions(det_all, ver) %>% filter(decision %in% c("reject", "relabel"))
  if (!nrow(bad)) return(data.frame())
  bad %>% group_by(common = original_common) %>%
    summarise(scientific = first(original_scientific), rejected = sum(decision == "reject"),
              relabelled_away = sum(decision == "relabel"), highest_rejected_conf = round(max(confidence), 3), .groups = "drop") %>%
    mutate(still_reported = ifelse(common %in% d$common, "yes", "no — all its calls were rejected or below threshold")) %>%
    select(common, scientific, rejected, relabelled_away, still_reported, highest_rejected_conf) %>%
    arrange(desc(rejected)) %>% as.data.frame()
}
daily_table <- function(ev, d) {
  if (!nrow(ev)) return(data.frame(date = character(), site = character(), common = character(), call_events = integer(), detections = integer()))
  e <- ev %>% mutate(date = format(start_dt, "%Y-%m-%d")) %>% count(date, site, common, name = "call_events")
  k <- d %>% count(date, site, common, name = "detections")
  full_join(e, k, by = c("date", "site", "common")) %>% mutate(across(c(call_events, detections), ~ coalesce(.x, 0L))) %>% as.data.frame()
}
sites_table <- function(files, d, ev) {
  a <- files[files$status == "analysed", ]
  if (!nrow(a) || !"site" %in% names(a)) return(data.frame())
  a %>% group_by(site) %>%
    summarise(lat = first(lat), lon = first(lon), location_source = first(location_source), device = first(device),
              files = n(), hours = round(sum(duration_s) / 3600, 2), first_recording = min(start_local),
              last_recording = max(start_local + duration_s), .groups = "drop") %>%
    left_join(d %>% group_by(site) %>% summarise(species = n_distinct(common), detections = n(), .groups = "drop"), by = "site") %>%
    left_join(ev %>% count(site, name = "call_events"), by = "site") %>%
    mutate(across(c(species, detections, call_events), ~ coalesce(.x, 0L))) %>% as.data.frame()
}
compute <- function(det_all, ver, files, s) {
  d <- filter_det(det_all, ver, s)
  ev <- call_events(d, s$gap)
  a <- files[files$status == "analysed", ]
  hours <- if (nrow(a)) sum(a$duration_s) / 3600 else 0
  list(det = d, ev = ev, hours = hours, files = files, det_all = det_all, ver = ver,
       summary = species_summary(d, ev, det_all, ver, hours), daily = daily_table(ev, d), sites = sites_table(files, d, ev),
       rejected = rejected_table(det_all, ver, d))
}
review_sample <- function(pool, n, order, seed, species, status) {
  d <- pool
  if (length(species)) d <- d[d$original_common %in% species, ]
  if (status == "pending") d <- d[d$decision == "pending", ]
  if (!nrow(d)) return(d)
  set.seed(seed)
  out <- lapply(split(d, d$original_common), function(g) {
    g <- g[order(g$confidence), ]; k <- min(n, nrow(g))
    switch(order, lowest = head(g, k), highest = tail(g, k), random = g[sample(nrow(g), k), ],
           g[unique(round(seq(1, nrow(g), length.out = k))), ])
  })
  out <- do.call(rbind, out)
  out[order(out$original_common, out$confidence), ]
}

# ------------------------------------------------------------------ plots
theme_bc <- function() theme_minimal(base_size = 11) + theme(
  plot.background = element_rect(fill = SURF, colour = NA), panel.background = element_rect(fill = SURF, colour = NA),
  panel.grid.minor = element_blank(), panel.grid.major = element_line(colour = GRID, linewidth = 0.4),
  axis.text = element_text(colour = INK2), axis.title = element_text(colour = INK2, size = 9),
  plot.title = element_text(face = "bold", colour = INK, size = 12), plot.title.position = "plot")
empty_plot <- function(msg = "No detections match the current filters")
  ggplot() + annotate("text", x = 0, y = 0, label = msg, colour = "#8a8984") + theme_void()

plot_species <- function(s) {
  if (!nrow(s)) return(empty_plot())
  s <- head(s[order(-s$call_events), ], 30)
  s$lab <- factor(paste0(s$common, "  (", s$species_id, ")"), levels = rev(paste0(s$common, "  (", s$species_id, ")")))
  s$verified_events <- coalesce(s$verified_events, 0L)
  long <- rbind(data.frame(lab = s$lab, n = s$verified_events, part = "verified by listening"),
                data.frame(lab = s$lab, n = s$call_events - s$verified_events, part = "not verified (BirdNET only)"))
  long$part <- factor(long$part, levels = c("not verified (BirdNET only)", "verified by listening"))
  ggplot() + geom_col(data = long, aes(n, lab, fill = part), width = 0.6, colour = "white", linewidth = 0.5) +
    scale_fill_manual(values = c(`verified by listening` = BLUE_D, `not verified (BirdNET only)` = TEAL_MID), name = NULL) +
    geom_text(data = s, aes(call_events, lab, label = sprintf("  %d  (%d det.%s)", call_events, detections,
              ifelse(verified_events > 0, paste0(", ", verified_events, " verified"), ""))), hjust = 0, size = 3, colour = INK2) +
    scale_x_continuous(expand = expansion(mult = c(0, 0.3))) +
    labs(title = "Calls per species", x = "Call events (detections merged when consecutive)", y = NULL) + theme_bc() +
    theme(panel.grid.major.y = element_blank(), legend.position = "bottom")
}
plot_hourly <- function(ev) {
  if (!nrow(ev)) return(empty_plot())
  h <- data.frame(hour = 0:23) %>% left_join(count(ev, hour = as.integer(format(start_dt, "%H"))), by = "hour")
  ggplot(h, aes(hour, coalesce(n, 0L))) + geom_col(fill = BLUE, width = 0.8) + scale_x_continuous(breaks = seq(0, 22, 2)) +
    labs(title = "Activity by hour of day (all species)", x = "Hour of day (local)", y = "Call events") + theme_bc()
}
plot_conf <- function(det, thr) {
  if (!nrow(det)) return(empty_plot("No detections yet"))
  det$kept <- det$confidence >= thr
  ggplot(det, aes(confidence, fill = kept)) + geom_histogram(breaks = seq(0, 1, 0.05), colour = "white") +
    scale_fill_manual(values = c(`TRUE` = BLUE, `FALSE` = BLUE_L), guide = "none") + geom_vline(xintercept = thr, colour = INK) +
    annotate("text", x = thr, y = Inf, label = sprintf("  threshold %.2f", thr), hjust = 0, vjust = 1.5, size = 3) +
    labs(title = "Confidence distribution (dark = kept)", x = "BirdNET confidence", y = "Detections") + theme_bc()
}
plot_timeline <- function(d, files) {
  if (!nrow(d)) return(empty_plot())
  ord <- d %>% group_by(common) %>% summarise(t = min(datetime)) %>% arrange(desc(t)) %>% pull(common)
  d$common <- factor(d$common, levels = ord)
  a <- files[files$status == "analysed" & !is.na(files$start_local), ]
  p <- ggplot()
  if (nrow(a)) p <- p + geom_rect(data = a, aes(xmin = start_local, xmax = start_local + duration_s, ymin = -Inf, ymax = Inf), fill = NEUTRAL)
  d$status <- ifelse(d$decision %in% c("confirm", "relabel"), "verified by listening", "BirdNET only")
  p + geom_point(data = d, aes(datetime, common, size = confidence, shape = status, colour = status), stroke = 1.1) +
    scale_shape_manual(values = c(`verified by listening` = 16, `BirdNET only` = 1), name = NULL) +
    scale_colour_manual(values = c(`verified by listening` = BLUE_D, `BirdNET only` = BLUE), name = NULL) +
    scale_size(range = c(1.5, 5), guide = "none") +
    labs(title = "When each species called (shaded = recorded periods; dot size = confidence; filled = verified)", x = NULL, y = NULL) +
    theme_bc() + theme(legend.position = "bottom")
}
plot_daily <- function(daily) {
  if (!nrow(daily)) return(empty_plot())
  daily <- daily %>% group_by(date, common) %>% summarise(call_events = sum(call_events), .groups = "drop")
  ggplot(daily, aes(date, reorder(common, call_events, sum), fill = call_events)) + geom_tile(colour = "white", linewidth = 1) +
    geom_text(aes(label = call_events, colour = call_events > max(call_events) * 0.55), size = 3.2) +
    scale_colour_manual(values = c(`TRUE` = "white", `FALSE` = INK), guide = "none") +
    scale_fill_gradientn(colours = SEQ, limits = c(0, NA)) +
    labs(title = "Call events per species per day", x = NULL, y = NULL, fill = "calls") + theme_bc() + theme(panel.grid = element_blank())
}
plot_freq <- function(s) {
  s <- s[!is.na(s$peak_hz), ]
  if (!nrow(s)) return(empty_plot())
  s$common <- factor(s$common, levels = s$common[order(s$peak_hz)])
  ggplot(s, aes(y = common)) + geom_segment(aes(x = low_hz / 1000, xend = high_hz / 1000, yend = common), colour = BLUE_L, linewidth = 3) +
    geom_point(aes(x = peak_hz / 1000), colour = BLUE_D, size = 3) +
    geom_text(aes(x = peak_hz / 1000, label = sprintf("%.1f kHz", peak_hz / 1000)), vjust = -1.1, size = 2.8, colour = INK2) +
    labs(title = "Call frequency (pitch) per species", y = NULL,
         x = "Frequency (kHz) — dot = median dominant frequency, bar = median 5–95 % energy band") + theme_bc()
}
plot_gaps <- function(ev) {
  g <- ev$gap_to_next_s[!is.na(ev$gap_to_next_s)]
  if (!length(g)) return(empty_plot("No repeated calls within a recording — no intervals to show"))
  ggplot(data.frame(g = g), aes(g)) + geom_histogram(bins = min(30, max(5, length(g))), fill = BLUE, colour = "white") +
    labs(title = "Time between calls", x = "Silence between consecutive calls of the same species (s, within one recording)", y = "Count") + theme_bc()
}

# ------------------------------------------------------------------ UI (same CSS/JS as app.py)
# Theme CSS + page JavaScript come from app.py, so both apps look and behave the same
ASSETS <- fromJSON(py("assets")$stdout, simplifyVector = TRUE)
CSS <- ASSETS$css; JS <- ASSETS$js

nav_footer <- function(back = NULL, fwd = NULL) HTML(sprintf('<div class="bc-next">%s%s</div>',
  if (is.null(back)) "<span></span>" else sprintf('<a href="#" class="back" data-goto="%s">‹ Back: %s</a>', back[1], back[2]),
  if (is.null(fwd)) "<span></span>" else sprintf('<a href="#" class="fwd" data-goto="%s">Next: %s ›</a>', fwd[1], fwd[2])))
hero <- function() {
  call <- ASSETS$call
  call_html <- if (is.null(call)) "" else sprintf(paste0(
    '<div class="bc-call" role="group" aria-label="Australian King-Parrot call"><button type="button" aria-label="Play">▶</button>',
    '<div class="body"><span class="who">🦜 Australian King-Parrot <small>· %.0f s · tap to play</small></span><div class="wave" title="Click to jump">%s</div></div>',
    '<audio preload="metadata" src="static/king_parrot_call.m4a"></audio></div>',
    '<div class="bc-credit">Call: <a href="%s" target="_blank" rel="noopener">%s</a></div>'),
    call$duration, paste0(sprintf('<i style="height:%d%%"></i>', pmax(8L, as.integer(unlist(call$peaks) * 100))), collapse = ""),
    call$url, htmlEscape(call$credit))
  photo <- if (file.exists(file.path("www", "king_parrot.jpg"))) sprintf(paste0(
    '<figure><img src="static/king_parrot.jpg" alt="Male Australian King-Parrot feeding in a berry tree">',
    '<figcaption>Male Australian King-Parrot · photo © %s</figcaption></figure>'), ABOUT$author) else "<span></span>"
  div(class = "bc-hero", HTML(photo),
      div(h2("Hear who's calling."),
          p(class = "lead", "Drop in your recorder's files and BirdNET lists every bird it hears: when, how often, how long and at what pitch. Then check the uncertain calls by ear, and take home a report."),
          HTML(call_html),
          div(class = "ctas", actionButton("start", "Upload my recordings ›", class = "btn-primary"),
              if (ASSETS$demo > 0) actionButton("demo", sprintf("🦜 Try with demo data (%d recordings)", ASSETS$demo), class = "btn-outline-primary"))))
}

kpi <- function(v, k) div(class = "bc-kpi", div(class = "v", v), div(class = "k", k))
datalist <- HTML(paste0('<datalist id="bc-labels">', paste0('<option value="', htmlEscape(LABELS, TRUE), '">', collapse = ""), "</datalist>"))
tz_choices <- sort(OlsonNames())

ui <- page_fluid(
  theme = bs_theme(version = 5),
  tags$head(tags$link(rel = "preconnect", href = "https://fonts.googleapis.com"),
            tags$link(rel = "stylesheet", href = "https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&family=Righteous&display=swap"),
            tags$style(HTML(CSS)), tags$title(APP_TITLE)),
  div(class = "bc-stripe"),
  div(class = "bc-header", h1(APP_TITLE), span(class = "bc-ver", paste0("v", APP_VERSION)),
      span(class = "sub", paste(ABOUT$family, "· BirdNET call detection · human verification · reporting")),
      div(class = "tools",
          tags$button(type = "button", class = "btn btn-outline-secondary", onclick = "bcRestart()", title = "Delete this session's data and start fresh", "↺ Start over"),
          actionButton("end_session", "⏻ End session", class = "btn-outline-secondary", title = "Delete your data now and close the session"))),
  div(class = "bc-warn", span("⚠"),
      span(tags$b("Temporary workspace: "), "uploads, results and your verifications are deleted when this session ends ",
           "(closing or refreshing the tab, or losing connection). ", tags$b("Download the PDF report"),
           " (charts, tables, spectrograms), the CSV bundle and your site profile before you leave."),
      tags$a("Go to report ›", href = "#", `data-goto` = "report")),
  uiOutput("steps"),
  navset_tab(id = "tabs",
    nav_panel("About", value = "about", hero(),
      layout_columns(col_widths = c(6, 6),
        card(card_header("What this app does"), p(ABOUT$purpose),
             h6("Workflow"),
             tags$ol(tags$li(tags$b("Upload"), " recordings (many at once)."),
                     tags$li(tags$b("Process"), " with BirdNET in the background."),
                     tags$li(tags$b("Verify"), " low-confidence calls by ear: confirm, reject or relabel."),
                     tags$li(tags$b("Results"), ": species, IDs, counts per day, call times, durations, intervals and pitch, with a custom confidence threshold."),
                     tags$li(tags$b("Report"), ": download the PDF and CSV bundle.")),
             h6("What you need"), tags$ul(lapply(ABOUT$inputs, tags$li)),
             h6("Limits and runtime"), tags$ul(lapply(ABOUT$limits, tags$li))),
        card(card_header("Data, citation and reproducibility"),
             h6("Data provenance"),
             tags$ul(tags$li("Species model: BirdNET GLOBAL 6K v2.4 via birdnetlib — Kahl, Wood, Eibl & Klinck (2021) BirdNET: a deep learning solution for avian diversity monitoring. Ecological Informatics 61, 101236."),
                     tags$li("Expected-species filter: BirdNET location/week meta-model, using the recorder's GPS and date."),
                     tags$li("Start time and location: GUANO metadata written by the recorder, else the file name."),
                     tags$li("Every input file is fingerprinted (SHA-1) and listed in the report and CSV bundle.")),
             h6("How to cite"), tags$blockquote(class = "bc-cite", ABOUT$citation),
             p(class = "bc-note", "Machine-readable citation: CITATION.cff in the code repository. A DOI will be added after the first archived release."),
             h6("Licence"), p(ABOUT$licence),
             h6("Reproducibility"),
             p(class = "bc-note", "The session info lists the app version, R and Python package versions, and BirdNET model file checksums."),
             p(class = "bc-note", "Best downloaded after processing, when it also records your files and settings. It's on the Report tab too, and inside the CSV bundle."),
             downloadButton("dl_session", "Download session info", class = "btn-outline-primary"),
             h6("Demo data and pictures"),
             p(class = "bc-note", sprintf("Photo © %s. King-Parrot call: Petra Holland, iNaturalist 324241020, CC BY 4.0. Demo recordings: iNaturalist observations 324241020 (CC BY 4.0, Petra Holland), 199447984 and 199347288 (CC0, Rosalie Lawrence) — resampled, coordinates rounded to ~1 km.", ABOUT$author)))),
      card(card_header("Data dictionary — columns in the CSV bundle"),
           div(class = "bc-dict", tags$table(class = "table table-sm",
             tags$thead(tags$tr(tags$th("Table"), tags$th("Column"), tags$th("Unit"), tags$th("Meaning"))),
             tags$tbody(lapply(seq_len(nrow(ABOUT$dictionary)), function(i) with(ABOUT$dictionary[i, ],
               tags$tr(tags$td(table), tags$td(tags$code(column)), tags$td(unit), tags$td(description)))))))),
      nav_footer(NULL, c("upload", "Upload"))),
    nav_panel("1 · Upload", value = "upload", layout_columns(col_widths = c(4, 8),
      card(card_header("Upload recordings"),
           div(class = "bc-drop", div(class = "big", "⤓"), tags$b("Drag & drop recordings here"), div(class = "bc-note", "or"),
               fileInput("upload", NULL, multiple = TRUE, accept = paste0(".", AUDIO_EXT), width = "100%",
                         buttonLabel = "Choose files…", placeholder = "WAV · FLAC · OGG")),
           p(class = "bc-note", "Tip: select all files of a deployment at once. Recorder metadata (GUANO time and GPS) is read automatically."),
           fileInput("profile_up", "Site profile (optional)", accept = ".json", width = "100%", buttonLabel = "Load profile…",
                     placeholder = "site_profile.json from a past session"),
           uiOutput("profile_status"),
           uiOutput("cache_meter"),
           div(style = "display:flex;gap:8px;flex-wrap:wrap", actionButton("demo2", "🦜 Load demo data", class = "btn-outline-primary btn-sm"),
               actionButton("clear_ws", "Clear workspace", class = "btn-outline-secondary btn-sm"))),
      card(card_header("Files in this workspace"), uiOutput("files_note"), DTOutput("files_table"))),
      nav_footer(c("about", "About"), c("process", "Process"))),
    nav_panel("2 · Process", value = "process", layout_columns(col_widths = c(5, 7),
      card(card_header("BirdNET settings"),
           radioButtons("loc_mode", "Location (limits species to those expected there that week)",
                        c("From each file's recorder GPS (typed values fill gaps)" = "guano", "Use the typed coordinates for every file" = "manual", "No location filter (all 6,522 species)" = "none")),
           layout_columns(numericInput("lat", "Latitude", NA, step = 1e-4), numericInput("lon", "Longitude", NA, step = 1e-4)),
           radioButtons("ts_mode", "Recording start times are…", c("Local clock time (use as-is)" = "local", "UTC — convert to time zone below" = "utc")),
           uiOutput("ts_hint"),
           selectizeInput("tz", "Time zone", tz_choices, selected = DEFAULT_TZ),
           sliderInput("min_conf", "Detection floor (lowest confidence kept for review)", 0.05, 0.9, 0.1, step = 0.05),
           p(class = "bc-note bc-help", "BirdNET ignores anything scored below this. Lower = more candidate calls to review (and more false alarms). You can still raise the threshold in Results without re-running."),
           sliderInput("sensitivity", "Sensitivity", 0.5, 1.5, 1.0, step = 0.05),
           p(class = "bc-note bc-help", "How readily BirdNET reports a call. Higher finds fainter or distant calls but adds false alarms; 1.0 is BirdNET's default."),
           sliderInput("overlap", "Window overlap (s)", 0, 2.5, 0, step = 0.5),
           p(class = "bc-note bc-help", "BirdNET listens in 3-second windows. Overlap (e.g. 1.5 s) lets windows overlap so calls cut by a window edge aren't missed. It's slower and gives more detections per call.")),
      card(card_header("Run"),
           div(style = "display:flex;gap:8px", actionButton("run", "▶ Run BirdNET", class = "btn-primary"),
               actionButton("cancel", "Cancel", class = "btn-outline-secondary")),
           uiOutput("job_status"), uiOutput("job_check"), DTOutput("job_log"))),
      nav_footer(c("upload", "Upload"), c("verify", "Verify"))),
    nav_panel("3 · Verify", value = "verify", card(
      card_header("Listen to low-confidence calls and confirm, reject or relabel them"),
      layout_columns(col_widths = c(2, 3, 2, 3, 2),
        textInput("verifier", "Your name / initials", placeholder = "e.g. AR"),
        sliderInput("rev_band", "Confidence band to review", 0, 1, c(0.1, 0.5), step = 0.05),
        numericInput("rev_n", "Clips per species", 6, min = 1, max = 50),
        selectInput("rev_order", "Pick clips", c("Spread across band" = "spread", "Lowest first" = "lowest", "Random" = "random", "Highest first" = "highest")),
        selectInput("rev_status", "Show", c("All in sample" = "all", "Unverified only" = "pending"))),
      div(style = "display:flex;gap:12px;align-items:end;flex-wrap:wrap",
          selectizeInput("rev_species", "Species", NULL, multiple = TRUE, options = list(placeholder = "All species")),
          actionButton("reshuffle", "↻ New random sample", class = "btn-sm btn-outline-secondary")),
      uiOutput("rev_progress"), uiOutput("rev_cards"), datalist),
      nav_footer(c("process", "Process"), c("results", "Results"))),
    nav_panel("4 · Results", value = "results", div(class = "bc-subtabs", layout_sidebar(
      sidebar = sidebar(width = 290, open = "always",
        sliderInput("thr", "Confidence threshold", 0.05, 1, 0.5, step = 0.01),
        input_switch("override", "Always keep verified calls", TRUE),
        input_switch("verified_only", "Verified calls only", FALSE),
        input_switch("per_species", "Per-species thresholds (site profile)", TRUE),
        numericInput("gap", "Merge detections into one call if gap ≤ (s)", 0, min = 0, max = 30, step = 0.5),
        selectizeInput("species_sel", "Species", NULL, multiple = TRUE, options = list(placeholder = "All species")),
        hr(), downloadButton("dl_pdf", "PDF report", class = "btn-primary w-100"),
        downloadButton("dl_zip", "CSV bundle (.zip)", class = "btn-outline-primary w-100 mt-2")),
      uiOutput("kpis"),
      navset_card_underline(id = "res_tabs",
        nav_panel("Overview", plotOutput("p_species", height = "420px"),
                  layout_columns(plotOutput("p_hourly", height = "280px"), plotOutput("p_conf", height = "280px"))),
        nav_panel("Species & IDs", DTOutput("t_species"),
                  h6("Rejected by reviewer", style = "margin-top:14px"),
                  p(class = "bc-note", "Species whose calls you rejected or relabelled. Rejected calls are left out of all results; a species that disappears entirely still shows here."),
                  DTOutput("t_rejected")),
        nav_panel("Reliability",
          p(class = "bc-note", "How often BirdNET was right for each species, from your Confirm / Reject / Relabel checks. A learned threshold is the lowest confidence that reaches the target. It is applied when 'Per-species thresholds' is on; other species use the global threshold."),
          layout_columns(col_widths = c(4, 4, 4),
            sliderInput("target", "Target: share correct", 0.7, 0.99, 0.9, step = 0.01),
            numericInput("min_n", "Minimum checks per species", 5, min = 2, max = 50),
            textInput("profile_name", "Profile name", placeholder = "e.g. Balcony 2026")),
          DTOutput("t_rel"),
          div(style = "display:flex;gap:10px;align-items:end;flex-wrap:wrap",
              selectizeInput("rule_sp", "Species", NULL, width = "280px"),
              numericInput("rule_thr", "Threshold", 0.5, min = 0.05, max = 1, step = 0.05, width = "120px"),
              actionButton("rule_set", "Set threshold", class = "btn-sm btn-outline-primary"),
              actionButton("rule_block", "Block species", class = "btn-sm btn-outline-danger"),
              actionButton("rule_clear", "Use learned / clear", class = "btn-sm btn-outline-secondary")),
          downloadButton("dl_profile", "Download site profile", class = "btn-primary mt-2")),
        nav_panel("Sites", p(class = "bc-note", "One row per recording site (from each file's GPS). Darwin Core occurrences for GBIF / ALA are in the CSV bundle under darwin_core/."),
                  DTOutput("t_sites")),
        nav_panel("Timeline", plotOutput("p_timeline", height = "480px")),
        nav_panel("Daily counts", plotOutput("p_daily", height = "380px"), DTOutput("t_daily")),
        nav_panel("Call timing", plotOutput("p_gaps", height = "280px"), p(class = "bc-note", "Select a row to listen to that call."),
                  DTOutput("t_events"), uiOutput("event_player")),
        nav_panel("Frequencies", plotOutput("p_freq", height = "420px"),
                  p(class = "bc-note", "Frequency is measured on each detection after subtracting the recording's median background noise.")),
        nav_panel("Examples", uiOutput("examples")),
        nav_panel("Detections", p(class = "bc-note", "Select a row to listen."), DTOutput("t_det"), uiOutput("det_player")),
        nav_panel("Report", value = "report", uiOutput("report_panel"))),
      nav_footer(c("verify", "Verify"), NULL))))),
  tags$script(HTML(JS))
)

card_html <- function(r, sid) {
  base <- sprintf("ws/%s/job/clips/%s", sid, r$det_id)
  when <- if (is.na(r$datetime)) "time unknown" else format(r$datetime, "%Y-%m-%d %H:%M:%S")
  status <- switch(r$decision, pending = "Unverified", confirm = "✓ Confirmed", reject = "✗ Rejected",
                   relabel = paste("↺ Relabelled →", r$common), r$decision)
  sprintf('<div class="bc-card st-%s" id="card-%s"><div class="hd"><span>%s</span><span class="conf">%.2f</span></div>
<div class="sub"><i>%s</i> · %s · %s</div><img src="%s.png" loading="lazy" alt="Waveform and spectrogram">
<audio controls preload="none" src="%s.wav"></audio><div class="meta">%s · %s @ %.0f–%.0f s · peak %s Hz</div>
<div class="act"><button class="btn-ok" onclick="bcVerify(\'%s\',\'confirm\')">✓ Confirm</button>
<button class="btn-no" onclick="bcVerify(\'%s\',\'reject\')">✗ Reject</button>
<button class="btn btn-sm btn-light" onclick="bcVerify(\'%s\',\'clear\')" title="Undo">↶</button></div>
<div class="rl"><input list="bc-labels" id="rl-%s" placeholder="Relabel: type a species…"><button class="btn-rl" onclick="bcVerify(\'%s\',\'relabel\')">Relabel</button></div>
<div class="stt">%s</div></div>',
    r$decision, r$det_id, htmlEscape(r$original_common), r$confidence, htmlEscape(r$scientific), r$species_id, r$sci_code,
    base, base, when, htmlEscape(r$file), r$start_s, r$end_s, ifelse(is.na(r$peak_hz), "", r$peak_hz),
    r$det_id, r$det_id, r$det_id, r$det_id, r$det_id, htmlEscape(status))
}
cards <- function(d, sid) HTML(paste0('<div class="bc-grid">', paste(vapply(seq_len(nrow(d)), function(i) card_html(d[i, ], sid), ""), collapse = ""), "</div>"))
player <- function(r, sid) HTML(sprintf('<div class="bc-card" style="max-width:520px"><b>%s</b><img src="ws/%s/job/clips/%s.png"><audio controls autoplay src="ws/%s/job/clips/%s.wav"></audio></div>',
                                        htmlEscape(r$common), sid, r$det_id, sid, r$det_id))
dt <- function(df, sel = "none", h = "420px") datatable(df, filter = "top", selection = sel, rownames = FALSE,
                                                       options = list(pageLength = 25, scrollX = TRUE, scrollY = h, dom = "tip"))

# ------------------------------------------------------------------ server
server <- function(input, output, session) {
  sid <- paste(sample(c(0:9, letters[1:6]), 32, TRUE), collapse = "")
  ws <- file.path(CACHE_ROOT, sid)
  dir.create(file.path(ws, "audio"), recursive = TRUE)
  file.create(file.path(ws, ".alive"))
  sweep_cache(sid)
  proc <- NULL; beat <- 0; last_state <- "init"; choice_cache <- list()
  manifest <- reactiveVal(data.frame())
  ver_bump <- reactiveVal(0); rev_seed <- reactiveVal(1); downloaded <- reactiveVal(FALSE); profile_bump <- reactiveVal(0)

  session$onSessionEnded(function() {
    dir.create(file.path(ws, "job"), showWarnings = FALSE); file.create(file.path(ws, "job", "cancel"))
    if (!is.null(proc) && proc$is_alive()) { proc$kill() }
    unlink(ws, recursive = TRUE)
  })
  send_quota <- function() session$sendCustomMessage("bc-quota", list(remaining = max(CACHE_LIMIT - dir_size(CACHE_ROOT), 0), limit = CACHE_LIMIT))
  showModal(modalDialog(title = "Your data is temporary", easyClose = FALSE, footer = modalButton("I understand"),
    p("This app keeps your recordings and results only for this browser session."),
    tags$ul(tags$li("Everything is deleted when you close or refresh this tab, or the connection drops."),
            tags$li("Before leaving, open ", tags$b("4 · Results → Report"), " and download the PDF report, the CSV bundle and your site profile (to reuse your checks next time)."),
            tags$li(sprintf("The server shares a %.0f GB temporary cache between all users.", CACHE_LIMIT / 1024^3)))))
  send_quota()

  read_manifest <- function() { m <- read_json_safe(file.path(ws, "manifest.json")); if (is.null(m) || !length(m)) data.frame() else m }

  # ----- upload
  observeEvent(input$upload, {
    up <- input$upload
    sweep_cache(sid)
    if (dir_size(CACHE_ROOT) + sum(up$size) > CACHE_LIMIT) {
      showNotification(sprintf("Not enough space in the %.0f GB cache for %s. Try fewer files.", CACHE_LIMIT / 1024^3, fmt_bytes(sum(up$size))), type = "error", duration = 10)
      return()
    }
    ok <- tolower(tools::file_ext(up$name)) %in% AUDIO_EXT
    withProgress(message = "Saving uploads…", value = 0, {
      for (i in which(ok)) { file.copy(up$datapath[i], file.path(ws, "audio", basename(up$name[i])), overwrite = TRUE); incProgress(1 / (sum(ok) + 1), detail = up$name[i]) }
      setProgress(message = "Reading recorder metadata…"); py("inspect", ws)
    })
    m <- read_manifest(); manifest(m)
    if (nrow(m) && is.na(input$lat) && any(!is.na(m$lat))) {
      i <- which(!is.na(m$lat))[1]; updateNumericInput(session, "lat", value = m$lat[i]); updateNumericInput(session, "lon", value = m$lon[i])
    }
    bad <- sum(m$status != "ok")
    msg <- sprintf("Uploaded %d file(s), %s.%s", sum(ok), fmt_bytes(sum(up$size[ok])), if (bad) sprintf(" ⚠ %d empty/unreadable file(s) will be skipped.", bad) else "")
    showNotification(msg, type = if (bad) "warning" else "message", duration = 8)
    session$sendCustomMessage("bc-notify", list(title = "Upload complete", body = msg))
    send_quota()
  })
  observeEvent(input$clear_ws, {
    if (!is.null(proc) && proc$is_alive()) return(showNotification("Cancel the running job first.", type = "warning"))
    unlink(file.path(ws, c("audio", "job", "manifest.json", "verifications.csv", "report.pdf", "results.zip")), recursive = TRUE)
    dir.create(file.path(ws, "audio")); manifest(data.frame()); ver_bump(ver_bump() + 1)
    showNotification("Workspace cleared."); send_quota()
  })
  output$cache_meter <- renderUI({
    invalidateLater(15000); manifest()
    used <- dir_size(CACHE_ROOT)
    div(div(class = "bc-note", tags$b("Temporary cache"), sprintf(" — this session %s · all users %s of %s", fmt_bytes(dir_size(ws)), fmt_bytes(used), fmt_bytes(CACHE_LIMIT))),
        div(class = "bc-meter", div(style = sprintf("width:%.1f%%", min(100, 100 * used / CACHE_LIMIT)))))
  })
  output$files_note <- renderUI({
    m <- manifest()
    if (!nrow(m)) return(p(class = "bc-note", "No files yet."))
    ok <- m[m$status == "ok", ]; bad <- sum(m$status != "ok")
    tagList(p(class = "bc-note", sprintf("%d files · %d usable · %.2f h of audio · %s", nrow(m), nrow(ok), sum(ok$duration_s, na.rm = TRUE) / 3600, fmt_bytes(sum(m$size)))),
            if (bad) p(class = "bc-bad", sprintf("⚠ %d file(s) cannot be analysed (empty or unreadable). Check the recorder's card and battery — this often means the device stopped writing.", bad)))
  })
  output$files_table <- renderDT({
    m <- manifest(); req(nrow(m))
    m$size <- vapply(m$size, fmt_bytes, ""); m$start <- paste0(sub("T", " ", m$start), ifelse(m$start_is_utc, " UTC", ""))
    dt(m[, intersect(c("name", "status", "size", "duration_s", "samplerate", "start", "start_source", "lat", "lon", "note", "problem"), names(m))])
  })
  output$ts_hint <- renderUI({
    m <- manifest(); if (!nrow(m) || !any(m$start_is_utc %in% TRUE)) return(NULL)
    p(class = "bc-note", sprintf("ℹ The recorder labels its times as UTC (e.g. %s Z). Many recorders write local clock time with a 'Z' anyway — if the file names match your local time, keep 'Local clock time'.",
                                 sub("T", " ", m$start[which(m$start_is_utc)[1]])))
  })

  # ----- processing
  status <- reactivePoll(750, session,
    checkFunc = function() {
      if (as.numeric(Sys.time()) - beat > 30) { Sys.setFileTime(file.path(ws, ".alive"), Sys.time()); beat <<- as.numeric(Sys.time()) }
      c(mtime(file.path(ws, "job", "status.json")), if (is.null(proc)) NA else proc$is_alive())
    },
    valueFunc = function() {
      st <- read_json_safe(file.path(ws, "job", "status.json"))
      if (!is.null(st) && !is.null(proc) && !proc$is_alive() && st$state %in% c("queued", "running")) {
        log <- tryCatch(paste(tail(readLines(file.path(ws, "job", "worker.log")), 5), collapse = " "), error = function(e) "")
        st$state <- "error"; st$message <- paste("The worker stopped unexpectedly.", log)
      }
      st
    })
  observeEvent(input$run, {
    if (!is.null(proc) && proc$is_alive()) return(showNotification("Already running.", type = "warning"))
    m <- manifest()
    if (!nrow(m) || !any(m$status == "ok")) { showNotification("Upload at least one usable recording first.", type = "error"); return(nav_select("tabs", "upload")) }
    if (input$loc_mode == "manual" && (is.na(input$lat) || is.na(input$lon))) return(showNotification("Enter latitude and longitude, or pick another location option.", type = "error"))
    job <- file.path(ws, "job"); unlink(job, recursive = TRUE); dir.create(job)
    write_json(list(loc_mode = input$loc_mode, lat = if (is.na(input$lat)) NULL else input$lat, lon = if (is.na(input$lon)) NULL else input$lon,
                    ts_mode = input$ts_mode, tz = input$tz, min_conf = input$min_conf, sensitivity = input$sensitivity,
                    overlap = input$overlap, started = now_iso()), file.path(job, "params.json"), auto_unbox = TRUE, null = "null")
    proc <<- process$new(PYTHON, c(APP_PY, "process", ws), stdout = file.path(job, "worker.log"), stderr = "2>&1", cleanup = TRUE)
    downloaded(FALSE)
    updateSliderInput(session, "thr", min = input$min_conf, value = max(input$min_conf, input$thr))
    updateSliderInput(session, "rev_band", value = c(input$min_conf, max(input$min_conf + 0.05, 0.5)))
  })
  observeEvent(input$cancel, if (!is.null(proc) && proc$is_alive()) { file.create(file.path(ws, "job", "cancel")); showNotification("Cancelling after the current file…") })
  observe({
    st <- status(); s <- if (is.null(st)) "none" else st$state
    if (identical(s, last_state)) return()
    prev <- last_state; last_state <<- s
    if (prev == "init") return()
    if (s == "done") {
      showNotification(paste(st$message, "Next: verify low-confidence calls."), type = "message", duration = 12)
      session$sendCustomMessage("bc-notify", list(title = "Processing complete", body = st$message))
    } else if (s == "error") {
      showNotification(st$message, type = "error", duration = NULL)
      session$sendCustomMessage("bc-notify", list(title = "Processing failed", body = substr(st$message, 1, 200)))
    }
    send_quota()
  })
  output$job_status <- renderUI({
    st <- status()
    if (is.null(st)) return(p(class = "bc-note", "Not started. Settings on the left; the model runs in the background so you can keep working."))
    eta <- ""
    if (st$state == "running" && st$done > 0 && length(st$log)) eta <- paste(" · about", fmt_secs(mean(st$log$seconds) * (st$n_files - st$done)), "left")
    div(div(class = "bc-progress", div(style = sprintf("width:%.1f%%", st$pct))),
        p(class = switch(st$state, error = "bc-bad", done = "bc-good", ""), tags$b(toupper(st$state)),
          sprintf(" · %d/%d files · %d detections%s", st$done, st$n_files, st$n_detections, eta)),
        p(class = "bc-note", st$message))
  })
  output$job_check <- renderUI({
    st <- status(); if (is.null(st)) return(NULL)
    stage <- st$stage %||% "queued"; state <- st$state
    cur <- match(stage, c("queued", "loading", "analysing", "finishing", "done")) - 1; if (is.na(cur)) cur <- 0
    t0 <- as.POSIXct(sub("Z$", "", st$started), format = "%Y-%m-%dT%H:%M:%S", tz = "UTC")
    t1 <- if (!is.null(st$finished)) as.POSIXct(sub("Z$", "", st$finished), format = "%Y-%m-%dT%H:%M:%S", tz = "UTC") else Sys.time()
    elapsed <- if (is.na(t0)) "" else fmt_secs(as.numeric(difftime(t1, t0, units = "secs")))
    steps <- list(
      list(0, "📂", "Recordings checked", sprintf("%d usable · %d skipped", st$n_files %||% 0, st$n_skipped %||% 0)),
      list(1, "🧠", "BirdNET model loaded", "GLOBAL 6K v2.4"),
      list(2, "🎧", "Listening to recordings", sprintf("%d/%d files", st$done %||% 0, st$n_files %||% 0)),
      list(2, "✂️", "Clips & spectrograms cut", sprintf("%d detections", st$n_detections %||% 0)),
      list(3, "📊", "Results saved", ""),
      list(4, "🔔", if (identical(state, "done")) "Done — you've been notified" else "Done", elapsed))
    lis <- vapply(steps, function(x) {
      idx <- x[[1]]; label <- x[[3]]; detail <- x[[4]]
      if (state == "error" && idx >= cur) { cls <- if (idx == cur) "err" else ""; ic <- if (idx == cur) "❌" else "⬜" }
      else if (state == "cancelled" && idx >= cur) { cls <- if (idx == cur) "err" else ""; ic <- if (idx == cur) "⏹️" else "⬜" }
      else if (state == "queued" && idx == 0) { cls <- "now"; ic <- '<span class="spin">⏳</span>'; label <- "Waiting for a free slot"; detail <- "other users are processing" }
      else if (idx < cur || stage == "done") { cls <- "done"; ic <- "✅" }
      else if (idx == cur) { cls <- "now"; ic <- '<span class="spin">🔄</span>' }
      else { cls <- ""; ic <- "⬜" }
      sprintf('<li class="%s"><span class="ic">%s</span><span>%s %s</span><span class="ds">%s</span></li>', cls, ic, x[[2]], htmlEscape(label), htmlEscape(detail))
    }, "")
    HTML(sprintf('<ul class="bc-check" aria-live="polite">%s</ul>', paste(lis, collapse = "")))
  })

  # ----- demo data
  load_demo <- function() {
    f <- list.files("demo", pattern = "\\.wav$", full.names = TRUE)
    if (!length(f)) return(showNotification("No demo recordings are bundled with this installation.", type = "warning"))
    if (!is.null(proc) && proc$is_alive()) return(showNotification("Wait for the current run to finish (or cancel it) before loading demo data.", type = "warning"))
    file.copy(f, file.path(ws, "audio", basename(f)), overwrite = TRUE)
    py("inspect", ws); manifest(read_manifest())
    nav_select("tabs", "upload")
    showNotification(sprintf("🦜 Loaded %d demo recordings (King-Parrot and friends). Next: Process → Run.", length(f)), type = "message", duration = 8)
  }
  observeEvent(input$demo, load_demo())
  observeEvent(input$demo2, load_demo())

  # ----- end session / stop app
  observeEvent(input$end_session, showModal(modalDialog(
    title = "End this session?", easyClose = TRUE,
    p("This deletes your uploads, results and checks from the server now."),
    p(class = if (downloaded()) "bc-good" else "bc-bad", tags$b("Downloaded your PDF report, CSV bundle and site profile? "), "They're on ",
      tags$b("4 · Results → Report"), if (downloaded()) "." else " — not downloaded yet in this session."),
    footer = div(style = "display:flex;gap:8px", modalButton("Keep working"),
                 actionButton("end_confirm", "End session & delete data", class = "btn-danger"),
                 if (ALLOW_STOP) actionButton("stop_app", "Stop the app", class = "btn-outline-danger", title = "Local use: shuts the server down")))))
  finish <- function(stopped) {
    removeModal()
    if (!is.null(proc) && proc$is_alive()) proc$kill()
    unlink(ws, recursive = TRUE)
    session$sendCustomMessage("bc-ended", list(stopped = stopped, text = if (stopped)
      "The server has been shut down. Close this tab; start it again from your terminal." else
      "Your recordings, results and checks were deleted from the server."))
  }
  observeEvent(input$end_confirm, { finish(FALSE); session$onFlushed(function() session$close(), once = TRUE) })
  observeEvent(input$stop_app, if (ALLOW_STOP) { finish(TRUE); session$onFlushed(function() stopApp(), once = TRUE) })

  output$job_log <- renderDT({ st <- status(); req(st, length(st$log)); dt(as.data.frame(st$log), h = "300px") })

  # ----- data
  det_all <- reactivePoll(1000, session, function() c(mtime(file.path(ws, "job", "detections.csv")), mtime(file.path(ws, "job", "files.csv"))),
                          function() load_det(ws))
  files <- reactive({ det_all(); load_files(ws) })
  ver <- reactive({ ver_bump(); load_ver(ws) })
  observe({
    d <- with_decisions(det_all(), ver())
    keep_sp <- isolate(input$species_sel); keep_rev <- isolate(input$rev_species)
    sp <- sort(unique(d$common)); orig <- sort(unique(d$original_common))
    # Only update when the lists change, so verifying a clip doesn't redraw the review grid
    if (!identical(sp, choice_cache$sp)) { choice_cache$sp <<- sp; updateSelectizeInput(session, "species_sel", choices = sp, selected = intersect(keep_sp, sp)) }
    if (!identical(orig, choice_cache$orig)) { choice_cache$orig <<- orig; updateSelectizeInput(session, "rev_species", choices = orig, selected = intersect(keep_rev, orig)) }
  })
  observe(session$sendCustomMessage("bc-dirty", list(dirty = nrow(det_all()) > 0 && !downloaded())))

  # ----- verification
  observeEvent(input$reshuffle, { rev_seed(rev_seed() + 1); updateSelectInput(session, "rev_order", selected = "random") })
  pool <- reactive({ d <- with_decisions(det_all(), ver()); d[d$confidence >= input$rev_band[1] & d$confidence <= input$rev_band[2], ] })
  output$rev_progress <- renderUI({
    if (!nrow(det_all())) return(p(class = "bc-note", "Run processing first."))
    p0 <- pool(); done <- sum(p0$decision != "pending")
    div(p(class = "bc-note", sprintf("%d of %d detections in this band verified (%d species). Clips are volume-normalised for listening; headphones recommended.",
                                     done, nrow(p0), length(unique(p0$original_common)))),
        div(class = "bc-meter", div(style = sprintf("width:%.1f%%;background:var(--good)", 100 * done / max(nrow(p0), 1)))))
  })
  output$rev_cards <- renderUI({
    rev_seed(); input$rev_n; input$rev_order; input$rev_status; input$rev_species; input$rev_band; det_all()
    p0 <- isolate(pool())
    s <- review_sample(p0, input$rev_n %||% 6, input$rev_order, rev_seed(), input$rev_species, input$rev_status)
    if (!nrow(s)) return(p(class = "bc-note", sprintf("No detections between %.2f and %.2f.", input$rev_band[1], input$rev_band[2])))
    tagList(if (nrow(s) > 150) p(class = "bc-note", sprintf("Showing first 150 of %d clips — narrow by species.", nrow(s))), cards(head(s, 150), sid))
  })
  observeEvent(input$verify_action, {
    a <- input$verify_action
    if (!a$id %in% det_all()$det_id) return()
    new_sci <- new_common <- ""
    if (a$decision == "relabel") {
      lab <- parse_label(a$label)
      if (is.null(lab)) return(showNotification(sprintf("'%s' is not a BirdNET species. Pick one from the list.", a$label), type = "error"))
      new_sci <- lab$sci; new_common <- lab$common
    }
    r <- det_all()[det_all()$det_id == a$id, ][1, ]
    append_ver(ws, list(det_id = a$id, decision = a$decision, new_scientific = new_sci, new_common = new_common,
                        verifier = a$verifier %||% "", note = "", timestamp_utc = now_iso(), scientific = r$scientific,
                        common = r$common, confidence = r$confidence, file = r$file, start_s = r$start_s, audio_sha = r$audio_sha))
    txt <- switch(a$decision, confirm = "✓ Confirmed", reject = "✗ Rejected", relabel = paste("↺ Relabelled →", new_common), clear = "Unverified")
    session$sendCustomMessage("bc-card", list(id = a$id, decision = if (a$decision == "clear") "pending" else a$decision, text = txt))
    ver_bump(ver_bump() + 1); downloaded(FALSE)
  })

  # ----- results
  # ----- site profile (rules + past checks; file format and maths live in app.py)
  profile <- reactive({ profile_bump(); p <- read_json_safe(file.path(ws, "profile.json")); if (is.null(p)) list(name = "", species = list()) else p })
  observeEvent(input$profile_up, {
    info <- tryCatch(fromJSON(py("profile-import", ws, input$profile_up$datapath[1])$stdout),
                     error = function(e) { showNotification(paste("Could not load the profile:", conditionMessage(e)), type = "error", duration = 10); NULL })
    if (is.null(info)) return()
    profile_bump(profile_bump() + 1); ver_bump(ver_bump() + 1)
    updateTextInput(session, "profile_name", value = info$name)
    showNotification(sprintf("Loaded site profile '%s': %d species rule(s), %d past check(s).", if (nzchar(info$name)) info$name else "unnamed",
                             info$rules, info$verifications), type = "message", duration = 8)
  })
  output$profile_status <- renderUI({
    pr <- profile(); n_rules <- length(pr$species); n_ver <- nrow(ver())
    if (!nzchar(pr$name %||% "") && !n_rules && !n_ver)
      return(p(class = "bc-note", "No profile loaded. After verifying, download one from Results → Reliability to reuse your checks and thresholds next time."))
    p(class = "bc-good", sprintf("✓ Profile '%s': %d species rule(s), %d check(s) in use.", if (nzchar(pr$name %||% "")) pr$name else "unnamed", n_rules, n_ver))
  })
  rel <- reactive({
    det_all(); ver(); profile()
    out <- fromJSON(py("reliability", ws, input$target, input$min_n %||% 5)$stdout)
    if (!length(out)) data.frame(scientific = character(), action = character(), effective_threshold = numeric()) else out
  })
  output$t_rel <- renderDT({ r <- rel(); req(nrow(r)); dt(r[, setdiff(names(r), "scientific")], h = "300px") })
  observe({
    d <- det_all(); pr <- profile()
    sp <- setNames(d$common, d$scientific); if (length(pr$species)) sp[names(pr$species)] <- vapply(pr$species, function(x) x$common %||% "", "")
    sp <- sp[!duplicated(names(sp))]; sp <- sp[order(sp)]
    choices <- if (length(sp)) setNames(names(sp), paste0(sp, " (", names(sp), ")")) else character(0)
    if (!identical(choices, choice_cache$rule)) { choice_cache$rule <<- choices; updateSelectizeInput(session, "rule_sp", choices = choices, selected = isolate(input$rule_sp)) }
  })
  set_rule <- function(action, value = NULL) {
    if (!nzchar(input$rule_sp %||% "")) return(showNotification("Pick a species first.", type = "warning"))
    do.call(py, as.list(c("profile-set", ws, input$rule_sp, action, value)))
    profile_bump(profile_bump() + 1); downloaded(FALSE)
  }
  observeEvent(input$rule_set, set_rule("threshold", input$rule_thr %||% 0.5))
  observeEvent(input$rule_block, set_rule("block"))
  observeEvent(input$rule_clear, set_rule("allow"))
  profile_handler <- downloadHandler(
    filename = function() sprintf("site_profile_%s.json", gsub("[^A-Za-z0-9]+", "_", if (nzchar(input$profile_name %||% "")) input$profile_name else "site")),
    content = function(file) {
      args <- c("profile-export", ws, input$target, input$min_n %||% 5, file)
      if (nzchar(input$profile_name %||% "")) args <- c(args, input$profile_name)
      do.call(py, as.list(args))
    }, contentType = "application/json")
  output$dl_profile <- profile_handler; output$dl_profile2 <- profile_handler

  settings <- reactive({
    r <- rel()
    eff <- r[!is.na(r$effective_threshold), ]
    list(thr = input$thr, override = input$override, verified_only = input$verified_only,
         gap = input$gap %||% 0, species = input$species_sel %||% character(0),
         per_species = input$per_species, thresholds = as.list(setNames(eff$effective_threshold, eff$scientific)),
         blocked = as.list(r$scientific[r$action == "block"]), target = input$target, min_n = input$min_n %||% 5)
  })
  R <- reactive(compute(det_all(), ver(), files(), settings()))
  output$kpis <- renderUI({
    r <- R(); dec <- with_decisions(r$det_all, r$ver)
    div(class = "bc-kpis", kpi(sum(r$files$status == "analysed"), sprintf("files analysed · %.1f h audio", r$hours)),
        kpi(length(unique(r$det$common)), "species"), kpi(nrow(r$det), "detections (3-s windows)"),
        kpi(nrow(r$ev), "call events"), kpi(sum(dec$decision != "pending"), sprintf("verified of %d raw", nrow(dec))))
  })
  output$p_species <- renderPlot(plot_species(R()$summary), res = 96)
  output$p_hourly <- renderPlot(plot_hourly(R()$ev), res = 96)
  output$p_conf <- renderPlot({ d <- with_decisions(det_all(), ver()); plot_conf(d[d$decision != "reject", ], input$thr) }, res = 96)
  output$p_timeline <- renderPlot(plot_timeline(R()$det, files()), res = 96)
  output$p_daily <- renderPlot(plot_daily(R()$daily), res = 96)
  output$p_freq <- renderPlot(plot_freq(R()$summary), res = 96)
  output$p_gaps <- renderPlot(plot_gaps(R()$ev), res = 96)
  fmt_dt <- function(x) format(x, "%Y-%m-%d %H:%M:%S")
  output$t_species <- renderDT({ s <- R()$summary; req(nrow(s)); s$first_heard <- fmt_dt(s$first_heard); s$last_heard <- fmt_dt(s$last_heard); dt(s, h = "480px") })
  output$t_daily <- renderDT(dt(R()$daily, h = "260px"))
  output$t_rejected <- renderDT({ r <- R()$rejected; req(nrow(r)); dt(r, h = "180px") })
  output$t_sites <- renderDT({ st <- R()$sites; req(nrow(st)); st$first_recording <- fmt_dt(st$first_recording); st$last_recording <- fmt_dt(st$last_recording); dt(st, h = "260px") })
  events_view <- reactive({ ev <- R()$ev; ev$start <- fmt_dt(ev$start_dt); ev$end <- format(ev$end_dt, "%H:%M:%S"); ev })
  output$t_events <- renderDT(dt(events_view()[, c("common", "species_id", "start", "end", "duration_s", "windows", "max_conf", "gap_to_next_s", "verified", "site", "file", "start_s")], "single"))
  output$event_player <- renderUI({
    i <- input$t_events_rows_selected; req(i)
    ev <- events_view()[i, ]; d <- R()$det
    hit <- d[d$file == ev$file & d$common == ev$common & d$start_s >= ev$start_s, ]
    if (nrow(hit)) player(hit[order(hit$start_s), ][1, ], sid)
  })
  det_view <- reactive({ d <- R()$det; d$datetime <- fmt_dt(d$datetime); d })
  output$t_det <- renderDT(dt(det_view()[, c("datetime", "common", "scientific", "species_id", "sci_code", "confidence", "decision",
                                             "peak_hz", "low_hz", "high_hz", "snr_db", "site", "lat", "lon", "file", "start_s", "det_id")], "single", "440px"))
  output$det_player <- renderUI({ i <- input$t_det_rows_selected; req(i); player(det_view()[i, ], sid) })
  output$examples <- renderUI({
    d <- R()$det
    if (!nrow(d)) return(p(class = "bc-note", "No detections match the current filters."))
    best <- d %>% group_by(common) %>% slice_max(confidence, n = 3, with_ties = FALSE) %>% ungroup() %>% arrange(common, desc(confidence)) %>% as.data.frame()
    cards(best, sid)
  })
  output$report_panel <- renderUI({
    r <- R()
    div(h4("Download before you leave"),
        p("The PDF uses the filters currently set in the sidebar. It includes: summary and settings, species table with IDs, calls per species, timeline, daily counts, hourly activity, frequency ranges, time between calls, confidence distribution, call timestamps, example spectrograms, your verification log and the file list."),
        p(class = "bc-note", sprintf("Current selection: %d species, %d detections, %d call events.", length(unique(r$det$common)), nrow(r$det), nrow(r$ev))),
        div(style = "display:flex;gap:10px", downloadButton("dl_pdf2", "Download PDF report", class = "btn-primary"),
            downloadButton("dl_zip2", "Download CSV bundle", class = "btn-outline-primary"),
            downloadButton("dl_profile2", "Download site profile", class = "btn-outline-primary"),
            downloadButton("dl_session2", "Session info", class = "btn-outline-secondary")),
        p(class = "bc-note", style = "margin-top:8px", "The site profile is a small JSON file holding your checks and per-species thresholds. Load it on the Upload tab next time so the app starts from what you already verified."),
        p(style = "margin-top:10px", class = if (downloaded()) "bc-good" else "bc-bad",
          if (downloaded()) "✓ Downloaded in this session." else "Not downloaded yet — your data will be lost when this session ends."))
  })
  settings_file <- function() { f <- file.path(ws, "settings.json"); write_json(settings(), f, auto_unbox = TRUE); f }
  pdf_handler <- downloadHandler(
    filename = function() sprintf("bruting_calls_report_%s.pdf", format(Sys.time(), "%Y%m%d_%H%M")),
    content = function(file) {
      withProgress(message = "Building PDF report…", detail = "charts, tables and spectrograms", py("report", ws, settings_file(), file))
      downloaded(TRUE)
    }, contentType = "application/pdf")
  zip_handler <- downloadHandler(
    filename = function() sprintf("bruting_calls_results_%s.zip", format(Sys.time(), "%Y%m%d_%H%M")),
    content = function(file) py("zip", ws, settings_file(), file), contentType = "application/zip")
  output$dl_pdf <- pdf_handler; output$dl_pdf2 <- pdf_handler
  output$dl_zip <- zip_handler; output$dl_zip2 <- zip_handler

  observeEvent(input$start, nav_select("tabs", "upload"))
  output$dl_session <- output$dl_session2 <- downloadHandler(
    filename = function() paste0("session_info_", gsub(" ", "_", APP_TITLE), "_v", APP_VERSION, "_", format(Sys.time(), "%Y%m%d_%H%M%S"), ".txt"),
    content = function(file) {
      m <- manifest()
      r_info <- if (requireNamespace("sessioninfo", quietly = TRUE)) capture.output(sessioninfo::session_info()) else capture.output(sessionInfo())
      writeLines(c(py("sessioninfo")$stdout, "", "── R session (front-end) ──", r_info, "", "── This session ──",
                   sprintf("Workspace files: %d", nrow(m)),
                   if (nrow(m)) sprintf("  %s  sha1 %s  %s", m$name, ifelse(is.na(m$sha1), "–", m$sha1), m$status),
                   paste("Processing parameters:", tryCatch(paste(readLines(file.path(ws, "job", "params.json"), warn = FALSE), collapse = ""),
                                                            error = function(e) "{}", warning = function(w) "{}")),
                   paste("Report settings:", toJSON(settings(), auto_unbox = TRUE))), file)
    })

  # ----- workflow bar
  observeEvent(input$goto, {
    if (input$goto == "report") { nav_select("tabs", "results"); nav_select("res_tabs", "report") } else nav_select("tabs", input$goto)
  })
  output$steps <- renderUI({
    m <- manifest(); st <- status(); d <- det_all()
    n_ok <- if (nrow(m)) sum(m$status == "ok") else 0
    s <- if (is.null(st)) "" else st$state
    p0 <- if (nrow(d)) pool() else data.frame(decision = character())
    n_ver <- sum(p0$decision != "pending")
    items <- list(
      list("upload", "Upload", if (n_ok) "done" else "active", if (nrow(m)) sprintf("%d usable file(s)", n_ok) else "add recordings", NA),
      list("process", "Process", switch(s, done = "done", error = "error", running = "active", queued = "active", if (n_ok) "active" else ""),
           switch(s, running = sprintf("%.0f%% · %d/%d", st$pct, st$done, st$n_files), queued = "queued…", done = sprintf("%d detections", st$n_detections),
                  error = "failed", cancelled = "cancelled", if (n_ok) "ready" else "waiting"), if (s == "running") st$pct else NA),
      list("verify", "Verify", if (nrow(p0) && n_ver == nrow(p0)) "done" else if (s == "done") "active" else "",
           if (nrow(p0)) sprintf("%d/%d checked", n_ver, nrow(p0)) else "low-confidence clips", NA),
      list("results", "Results", if (s == "done" && nrow(d)) "done" else if (s == "done") "active" else "",
           if (nrow(d)) sprintf("%d species (raw)", length(unique(d$common))) else "charts & tables", NA),
      list("report", "Report", if (downloaded()) "done" else if (s == "done") "active" else "", if (downloaded()) "downloaded ✓" else "download PDF", NA))
    lis <- vapply(seq_along(items), function(i) {
      it <- items[[i]]; mark <- if (it[[3]] == "done") "✓" else if (it[[3]] == "error") "!" else i
      bar <- if (!is.na(it[[5]])) sprintf('<div class="mini" style="width:%.0f%%"></div>', it[[5]]) else ""
      run <- if (it[[1]] == "process" && s %in% c("running", "queued")) " bc-running" else ""
      sprintf('<li class="bc-step %s%s" data-goto="%s" title="Go to %s"><span class="dot">%s</span><span><div class="lbl">%s</div><div class="st">%s</div></span>%s</li>',
              it[[3]], run, it[[1]], it[[2]], mark, it[[2]], htmlEscape(it[[4]]), bar)
    }, "")
    HTML(paste0('<ol class="bc-steps">', paste(lis, collapse = ""), "</ol>"))
  })
}

shinyApp(ui, server)
