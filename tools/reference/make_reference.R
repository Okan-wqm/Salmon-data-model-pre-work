# Reference values from base R for tools/test_analysis.py (see make_reference.py).
# Split-plot ANOVA with two error strata: temperature is tested against systems, diet and
# diet x temperature against the residual between the tanks of a system.
# Run from the repository root: Rscript tools/reference/make_reference.R
here <- "tools/reference"
d <- read.csv(file.path(here, "unit_responses_seed7.csv"), stringsAsFactors = TRUE)
d$diet <- relevel(d$diet, "D1")
d$temperature <- relevel(d$temperature, "12C")
labels <- setdiff(names(d), c("unit", "system", "diet", "temperature"))
rn <- function(x) trimws(rownames(x))
term <- function(x, r) list(df = x[rn(x) == r, "Df"], ms = x[rn(x) == r, "Mean Sq"],
                            f = x[rn(x) == r, "F value"], p = x[rn(x) == r, "Pr(>F)"])
err <- function(x) list(df = x[rn(x) == "Residuals", "Df"], ms = x[rn(x) == "Residuals", "Mean Sq"])
ref <- list()
for (lb in labels) {
  s <- summary(aov(as.formula(paste(lb, "~ diet*temperature + Error(system)")), data = d))
  w <- s[["Error: system"]][[1]]
  u <- s[["Error: Within"]][[1]]
  ref[[lb]] <- list(temperature = term(w, "temperature"), system = err(w), diet = term(u, "diet"),
                    interaction = term(u, "diet:temperature"), residual = err(u))
}
out <- list(made_with = list(R = paste(R.version$major, R.version$minor, sep = ".")), aov = ref)
con <- file(file.path(here, "aov_reference.json"), "wb")          # binary: LF line ends on every platform
writeLines(jsonlite::toJSON(out, auto_unbox = TRUE, digits = NA, pretty = TRUE), con, sep = "\n")
close(con)
cat("wrote aov_reference.json for", paste(labels, collapse = ", "), "\n")
