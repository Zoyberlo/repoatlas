<?php

declare(strict_types=1);

/**
 * Makes the three classes loadable without a composer package.
 *
 * PHPStan builds its service container before it runs bootstrap files, but
 * instantiates services lazily, so a class registered in the neon only has
 * to exist by the time the analysis reaches it. Requiring them here is
 * enough, and it keeps this extension a directory rather than something
 * that has to be published to packagist to be usable.
 */

require_once __DIR__ . '/SiteCollector.php';
require_once __DIR__ . '/DeclarationCollector.php';
require_once __DIR__ . '/DumpRule.php';
