# R packages for app.R (the R front-end also needs the Python environment from requirements.txt)
pkgs <- c("shiny", "bslib", "ggplot2", "dplyr", "DT", "jsonlite", "processx", "htmltools", "sessioninfo", "renv")
missing <- setdiff(pkgs, rownames(installed.packages()))
if (length(missing)) install.packages(missing, repos = "https://cloud.r-project.org")
