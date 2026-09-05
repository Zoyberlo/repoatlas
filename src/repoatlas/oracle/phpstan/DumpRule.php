<?php

declare(strict_types=1);

namespace RepoAtlas\PhpStan;

use PhpParser\Node;
use PHPStan\Analyser\Scope;
use PHPStan\Node\CollectedDataNode;
use PHPStan\Rules\Rule;

/**
 * Writes what the collectors gathered, and reports nothing.
 *
 * PHPStan has one output channel, its list of errors, and this is not
 * error-shaped: it is a data extraction that happens to be driven by an
 * analyser. So the rule writes JSON Lines to a path given in the
 * environment and returns no errors at all, which keeps the exit code
 * meaning what it usually means. A run that finds real type errors in the
 * analysed project still dumps, because the dump is not conditional on the
 * project being clean, and on a real application it never is.
 *
 * @implements Rule<CollectedDataNode>
 */
final class DumpRule implements Rule
{
    public const PATH_VARIABLE = 'REPOATLAS_PHPSTAN_DUMP';

    public function getNodeType(): string
    {
        return CollectedDataNode::class;
    }

    /**
     * @return list<string>
     */
    public function processNode(Node $node, Scope $scope): array
    {
        $path = getenv(self::PATH_VARIABLE);
        if ($path === false || $path === '') {
            return [];
        }
        $handle = fopen($path, 'wb');
        if ($handle === false) {
            return [];
        }
        foreach ([SiteCollector::class, DeclarationCollector::class] as $collector) {
            /** @var array<string, list<list<array<string, mixed>>>> $byFile */
            $byFile = $node->get($collector);
            foreach ($byFile as $file => $groups) {
                foreach ($groups as $records) {
                    foreach ($records as $record) {
                        $record['path'] = $file;
                        $line = json_encode($record, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
                        if ($line !== false) {
                            fwrite($handle, $line . "\n");
                        }
                    }
                }
            }
        }
        fclose($handle);
        return [];
    }
}
