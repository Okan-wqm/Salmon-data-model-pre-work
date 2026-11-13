# Reference values from lme4 / lmerTest for tools/test_analysis.py and tools/build_dashboard.py.
# The same tank values as make_reference.R (unit_responses_seed7.csv, written by
# make_reference.py export), fitted as a mixed model with a random system intercept by REML:
#   lmer(y ~ diet*temperature + (1 | system))
# For the diet main effect (averaged over temperature), the temperature main effect (averaged over
# diets) and the interaction, it stores estimate, SE, denominator df, t and p with Satterthwaite and
# with Kenward-Roger degrees of freedom: the small-sample tests most mixed-model analyses report.
# Where REML puts the system variance at 0 (singular fit) the two error strata are pooled, so
# these tests then differ from the stratum tests of tools/analysis.py; the file records that too.
# Needs lme4, lmerTest, pbkrtest and jsonlite. Run from the repository root:
#   Rscript tools/reference/make_reference_lmer.R
suppressPackageStartupMessages({library(lme4); library(lmerTest)})
here <- "tools/reference"
d <- read.csv(file.path(here, "unit_responses_seed7.csv"), stringsAsFactors = TRUE)
d$diet <- relevel(d$diet, "D1")
d$temperature <- relevel(d$temperature, "12C")
labels <- setdiff(names(d), c("unit", "system", "diet", "temperature"))
# treatment coding: (Intercept), dietD2, temperature16C, dietD2:temperature16C
L <- rbind(diet = c(0, 1, 0, 0.5), temperature = c(0, 0, 1, 0.5), interaction = c(0, 0, 0, 1))
test <- function(fit, ddf) {
  out <- list()
  for (k in rownames(L)) {
    r <- contest1D(fit, L[k, ], ddf = ddf)
    out[[k]] <- list(estimate = r$Estimate, se = r$`Std. Error`, df = r$df, t = r$`t value`, p = r$`Pr(>|t|)`)
  }
  out
}
ref <- list()
for (lb in labels) {
  fit <- lmer(as.formula(paste(lb, "~ diet*temperature + (1 | system)")), data = d, REML = TRUE,
              control = lmerControl(check.conv.singular = "ignore"))
  vc <- as.data.frame(VarCorr(fit))
  ref[[lb]] <- list(singular = isSingular(fit), var_system = vc$vcov[vc$grp == "system"],
                    var_residual = vc$vcov[vc$grp == "Residual"],
                    satterthwaite = test(fit, "Satterthwaite"), kenward_roger = test(fit, "Kenward-Roger"))
}
v <- function(p) as.character(packageVersion(p))
out <- list(made_with = list(R = paste(R.version$major, R.version$minor, sep = "."), lme4 = v("lme4"),
                             lmerTest = v("lmerTest"), pbkrtest = v("pbkrtest")),
            inputs = as.list(d[, c("unit", labels)]), lmer = ref)
out$inputs$unit <- as.character(out$inputs$unit)
con <- file(file.path(here, "lmer_reference.json"), "wb")          # binary: LF line ends on every platform
writeLines(jsonlite::toJSON(out, auto_unbox = TRUE, digits = NA, pretty = TRUE), con, sep = "\n")
close(con)
cat("wrote lmer_reference.json (lme4", v("lme4"), "lmerTest", v("lmerTest"), ") for", paste(labels, collapse = ", "), "\n")
